#include "geometry.h"

#include <algorithm>
#include <cstdlib>

namespace ios_screen {

void placeMonitorToRight(const Rect& primaryBounds, int /*virtW*/, int virtH, int* outX, int* outY) {
  if (outX) *outX = primaryBounds.right();
  if (outY) *outY = primaryBounds.y + (primaryBounds.h - virtH) / 2;
}

Rect phoneAreaInWorkArea(const Rect& workArea, int phoneW, int phoneH, int titleBar) {
  Rect r;
  r.w = phoneW;
  r.h = phoneH;
  r.x = workArea.x;
  // 窗口高 = 手机高 + 标题栏；窗口底边贴可用区底边
  r.y = workArea.bottom() - phoneH;
  (void)titleBar;
  if (r.y < workArea.y) r.y = workArea.y;
  return r;
}

bool sameRect(const Rect& a, const Rect& b, int tolerance) {
  return std::abs(a.x - b.x) <= tolerance && std::abs(a.y - b.y) <= tolerance &&
         std::abs(a.w - b.w) <= tolerance && std::abs(a.h - b.h) <= tolerance;
}

}  // namespace ios_screen
