#include "edge_trigger.h"

#include <algorithm>

namespace ios_screen {

bool parseEdge(const std::string& s, Edge* out) {
  if (!out) return false;
  if (s == "right" || s == "r") { *out = Edge::Right; return true; }
  if (s == "left" || s == "l") { *out = Edge::Left; return true; }
  if (s == "top" || s == "t") { *out = Edge::Top; return true; }
  if (s == "bottom" || s == "b") { *out = Edge::Bottom; return true; }
  return false;
}

const char* edgeName(Edge e) {
  switch (e) {
    case Edge::Right: return "right";
    case Edge::Left: return "left";
    case Edge::Top: return "top";
    case Edge::Bottom: return "bottom";
  }
  return "?";
}

bool isAtEdge(const EdgeSpec& spec, int x, int y, int bandPx) {
  const Rect& r = spec.screen;
  // 只有落在屏内（或恰好压线）才谈得上"贴边"，屏外直接不算
  if (x < r.x || y < r.y || x >= r.right() || y >= r.bottom()) return false;
  switch (spec.edge) {
    case Edge::Right: return x >= r.right() - 1 - bandPx;
    case Edge::Left: return x <= r.x + bandPx;
    case Edge::Top: return y <= r.y + bandPx;
    case Edge::Bottom: return y >= r.bottom() - 1 - bandPx;
  }
  return false;
}

int outwardComponent(const EdgeSpec& spec, int rawDx, int rawDy) {
  switch (spec.edge) {
    case Edge::Right: return rawDx;   // 往右为外
    case Edge::Left: return -rawDx;
    case Edge::Top: return -rawDy;
    case Edge::Bottom: return rawDy;
  }
  return 0;
}

int inwardComponent(const EdgeSpec& spec, int rawDx, int rawDy) {
  return -outwardComponent(spec, rawDx, rawDy);
}

void EdgeTrigger::reset() {
  active_ = false;
  accum_ = 0;
}

EdgeTrigger::Change EdgeTrigger::update(const EdgeSpec& spec, int cursorX, int cursorY,
                                        int rawDx, int rawDy) {
  const int out = outwardComponent(spec, rawDx, rawDy);
  const int in = -out;

  if (!active_) {
    // 没贴边就不该攒进度：这是"想推出去"和"顺手划到边上"的分界线
    if (!isAtEdge(spec, cursorX, cursorY)) {
      accum_ = 0;
      return Change::None;
    }
    if (out > 0) {
      accum_ = std::min(accum_ + out, cfg_.enterThreshold * 4);
      if (accum_ >= cfg_.enterThreshold) {
        active_ = true;
        accum_ = 0;
        return Change::Entered;
      }
    } else {
      accum_ = std::max(0, accum_ - cfg_.decayPerEvent);
    }
    return Change::None;
  }

  // 捕获中：光标位置已经不代表用户意图（可能被我们挪走了），只看位移方向
  if (in > 0) {
    accum_ += in;
    if (accum_ >= cfg_.exitThreshold) {
      active_ = false;
      accum_ = 0;
      return Change::Exited;
    }
  } else if (out > 0 && accum_ > 0) {
    // 又往外推了：把退出进度吐回去，别让"进去-出来-进去"攒出误退出
    accum_ = std::max(0, accum_ - out);
  }
  return Change::None;
}

}  // namespace ios_screen
