package com.yellowstacks.launcher;

import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.drawable.ColorDrawable;
import android.graphics.drawable.Drawable;
import android.os.Bundle;
import android.text.TextUtils;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.AdapterView;
import android.widget.BaseAdapter;
import android.widget.FrameLayout;
import android.widget.GridView;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.text.Collator;
import java.util.ArrayList;
import java.util.Calendar;
import java.util.Collections;
import java.util.Comparator;
import java.util.List;

/**
 * The Yellowstacks home screen: every app that can be opened, as a tile.
 *
 * This is a real Android home app, so Android itself decides what is in front.
 * Pressing Home really does send the game to the background (its music stops),
 * and Recent apps really does list it - the Yellowstacks window never has to
 * hide anything or pretend.
 *
 * Every nested class here is static on purpose: the d8 in build tools 34 cannot
 * dex the inner classes that JDK 21 produces (it dies with a NullPointerException
 * in its InnerClasses handling), and static ones avoid the implicit outer
 * reference anyway.
 */
public class HomeActivity extends Activity implements AdapterView.OnItemClickListener {

    static final int SCRIM = 0xD8141317;   // over the wallpaper, so the tiles read
    static final int YELLOW = 0xFFFFD23F;
    static final int TEXT = 0xFFF3EBD3;
    static final int MUTED = 0xFF8A8578;
    static final int PRESSED = 0x26FFD23F;

    private final List<Entry> entries = new ArrayList<>();
    private Adapter adapter;
    private TextView clock;
    private PackageWatcher watcher;

    /** One app on the home screen. */
    static final class Entry {
        final String label;
        final Drawable icon;
        final ComponentName component;

        Entry(String label, Drawable icon, ComponentName component) {
            this.label = label;
            this.icon = icon;
            this.component = component;
        }
    }

    static int dp(Context context, float value) {
        return Math.round(TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, value,
                context.getResources().getDisplayMetrics()));
    }

    private int dp(float value) {
        return dp(this, value);
    }

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        setContentView(buildUi());
        load();

        watcher = new PackageWatcher(this);
        IntentFilter filter = new IntentFilter();
        filter.addAction(Intent.ACTION_PACKAGE_ADDED);
        filter.addAction(Intent.ACTION_PACKAGE_REMOVED);
        filter.addAction(Intent.ACTION_PACKAGE_CHANGED);
        filter.addDataScheme("package");
        registerReceiver(watcher, filter);
    }

    @Override
    protected void onDestroy() {
        if (watcher != null) {
            unregisterReceiver(watcher);
            watcher = null;
        }
        super.onDestroy();
    }

    @Override
    protected void onResume() {
        super.onResume();
        showTime();
        load();   // an app may have been installed or removed while we were away
    }

    /** Home is already home: there is nowhere for Back to go. */
    @Override
    public void onBackPressed() {
    }

    // ---------------------------------------------------------------- the screen

    private View buildUi() {
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(SCRIM);

        LinearLayout column = new LinearLayout(this);
        column.setOrientation(LinearLayout.VERTICAL);
        column.setFitsSystemWindows(true);
        root.addView(column, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        column.addView(buildHeader(), new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        GridView grid = new GridView(this);
        adapter = new Adapter(this, entries);
        grid.setAdapter(adapter);
        grid.setNumColumns(GridView.AUTO_FIT);
        grid.setColumnWidth(dp(104));
        grid.setStretchMode(GridView.STRETCH_COLUMN_WIDTH);
        grid.setVerticalSpacing(dp(10));
        grid.setPadding(dp(18), dp(6), dp(18), dp(18));
        grid.setClipToPadding(false);
        grid.setSelector(new ColorDrawable(Color.TRANSPARENT));
        grid.setVerticalScrollBarEnabled(false);
        grid.setOnItemClickListener(this);
        column.addView(grid, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
        return root;
    }

    private View buildHeader() {
        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setGravity(Gravity.CENTER_VERTICAL);
        bar.setPadding(dp(22), dp(14), dp(22), dp(10));

        ImageView logo = new ImageView(this);
        logo.setImageResource(R.mipmap.ic_launcher);
        bar.addView(logo, new LinearLayout.LayoutParams(dp(22), dp(22)));

        TextView name = new TextView(this);
        name.setText("Yellowstacks");
        name.setTextColor(TEXT);
        name.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13);
        name.setTypeface(null, android.graphics.Typeface.BOLD);
        LinearLayout.LayoutParams namePos = new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        namePos.leftMargin = dp(8);
        bar.addView(name, namePos);

        clock = new TextView(this);
        clock.setTextColor(MUTED);
        clock.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        bar.addView(clock, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        showTime();
        return bar;
    }

    private void showTime() {
        if (clock == null) {
            return;
        }
        Calendar now = Calendar.getInstance();
        clock.setText(String.format("%02d:%02d", now.get(Calendar.HOUR_OF_DAY),
                now.get(Calendar.MINUTE)));
    }

    // ---------------------------------------------------------------- the apps

    /** Every app with a launcher icon, by name, except this one. */
    void load() {
        PackageManager pm = getPackageManager();
        Intent main = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER);
        List<ResolveInfo> found = pm.queryIntentActivities(main, 0);
        List<Entry> fresh = new ArrayList<>();
        for (ResolveInfo info : found) {
            String pkg = info.activityInfo.packageName;
            if (getPackageName().equals(pkg)) {
                continue;
            }
            fresh.add(new Entry(info.loadLabel(pm).toString(), info.loadIcon(pm),
                    new ComponentName(pkg, info.activityInfo.name)));
        }
        Collections.sort(fresh, new ByLabel());
        entries.clear();
        entries.addAll(fresh);
        if (adapter != null) {
            adapter.notifyDataSetChanged();
        }
    }

    @Override
    public void onItemClick(AdapterView<?> parent, View view, int position, long id) {
        if (position >= 0 && position < entries.size()) {
            open(entries.get(position));
        }
    }

    private void open(Entry entry) {
        Intent intent = new Intent(Intent.ACTION_MAIN)
                .addCategory(Intent.CATEGORY_LAUNCHER)
                .setComponent(entry.component)
                .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK
                        | Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED);
        try {
            startActivity(intent);
        } catch (RuntimeException e) {
            load();   // the app went away between the list being built and the tap
        }
    }

    /** Apps by name, the way the user reads them. */
    static final class ByLabel implements Comparator<Entry> {
        private final Collator collator = Collator.getInstance();

        @Override
        public int compare(Entry a, Entry b) {
            return collator.compare(a.label, b.label);
        }
    }

    /** Keeps the grid in step as apps are installed and removed. */
    static final class PackageWatcher extends BroadcastReceiver {
        private final HomeActivity home;

        PackageWatcher(HomeActivity home) {
            this.home = home;
        }

        @Override
        public void onReceive(Context context, Intent intent) {
            home.load();
        }
    }

    // ---------------------------------------------------------------- the tiles

    static final class Adapter extends BaseAdapter {
        private final Context context;
        private final List<Entry> entries;

        Adapter(Context context, List<Entry> entries) {
            this.context = context;
            this.entries = entries;
        }

        @Override
        public int getCount() {
            return entries.size();
        }

        @Override
        public Object getItem(int position) {
            return entries.get(position);
        }

        @Override
        public long getItemId(int position) {
            return position;
        }

        @Override
        public View getView(int position, View recycled, ViewGroup parent) {
            Tile tile = recycled instanceof Tile ? (Tile) recycled : new Tile(context);
            tile.show(entries.get(position));
            return tile;
        }
    }

    /** Icon above its name, with a rounded highlight while it is held. */
    static final class Tile extends LinearLayout {
        private final ImageView icon;
        private final TextView label;
        private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final float radius;

        Tile(Context context) {
            super(context);
            icon = new ImageView(context);
            label = new TextView(context);
            radius = dp(context, 14);

            setOrientation(VERTICAL);
            setGravity(Gravity.CENTER_HORIZONTAL);
            setPadding(dp(context, 4), dp(context, 10), dp(context, 4), dp(context, 10));
            setWillNotDraw(false);

            addView(icon, new LayoutParams(dp(context, 56), dp(context, 56)));
            label.setTextColor(TEXT);
            label.setTextSize(TypedValue.COMPLEX_UNIT_SP, 11);
            label.setGravity(Gravity.CENTER_HORIZONTAL);
            label.setMaxLines(2);
            label.setEllipsize(TextUtils.TruncateAt.END);
            LayoutParams labelPos = new LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT);
            labelPos.topMargin = dp(context, 7);
            addView(label, labelPos);
            paint.setColor(PRESSED);
        }

        void show(Entry entry) {
            icon.setImageDrawable(entry.icon);
            label.setText(entry.label);
            setContentDescription(entry.label);
        }

        @Override
        protected void dispatchDraw(Canvas canvas) {
            if (isPressed()) {
                canvas.drawRoundRect(0, 0, getWidth(), getHeight(), radius, radius, paint);
            }
            super.dispatchDraw(canvas);
        }

        @Override
        public void setPressed(boolean pressed) {
            super.setPressed(pressed);
            invalidate();
        }
    }
}
