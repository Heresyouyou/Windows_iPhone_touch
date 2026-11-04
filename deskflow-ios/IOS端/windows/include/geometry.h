// 显示器/窗口几何。Windows 用的是物理像素，原点在主屏左上，没有"点"的概念，
// 所以这里全部用整数像素，不需要缩放因子。
#pragma once

namespace ios_screen {

struct Rect {
  int x = 0, y = 0, w = 0, h = 0;
  int right() const { return x + w; }
  int bottom() const { return y + h; }
  bool contains(const Rect& inner) const {
    return inner.x >= x && inner.y >= y && inner.right() <= right() && inner.bottom() <= bottom();
  }
  bool operator==(const Rect& o) const {
    return x == o.x && y == o.y && w == o.w && h == o.h;
  }
};

struct Monitor {
  Rect bounds;              // 整块屏
  Rect work;                // 去掉任务栏的可用区（Windows: rcWork）
  bool primary = false;
};

// 虚拟屏贴主屏右缘、垂直居中。返回的原点用于 ChangeDisplaySettingsEx 的 dmPosition。
void placeMonitorToRight(const Rect& primaryBounds, int virtW, int virtH, int* outX, int* outY);

// 手机画面区在虚拟屏可用区里的位置：底部对齐，上方留出标题栏。
// 之所以底部对齐而不是居中：macOS 会把窗口顶边钳在菜单栏以下，居中会被系统
// 反复推回去，造成"每 4 秒重摆一次"的抖动。Windows 没有这个约束，但保持同一
// 套几何便于两端对齐同一份坐标。
Rect phoneAreaInWorkArea(const Rect& workArea, int phoneW, int phoneH, int titleBar = 28);

// 差异比较：设置完必须回读校验，不要"设完就信"。
bool sameRect(const Rect& a, const Rect& b, int tolerance = 1);

}  // namespace ios_screen
