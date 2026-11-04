// 边缘触发：不用虚拟屏，靠 Raw Input 的相对位移判断"用户想把光标推出屏幕"。
//
// 为什么这条能成立：光标撞到屏幕边缘会被 WindowServer/内核钳制在屏内，
// 但 Raw Input 的 lLastX/lLastY 是设备级计数，和光标位置无关——FPS 游戏把
// 光标锁在屏幕中心还能无限转视角，靠的就是这个。所以"贴边 + 继续朝外"是
// 一个可靠信号，不需要真的存在第二块显示器。
//
// 这比 macOS 版省掉整整一层（CGVirtualDisplay / IddCx），代价是：
//   1. 必须自己判定边缘，不能依赖系统"光标能移出去"；
//   2. 必须自己处理"贴着边缘时的误触"，所以进入捕获后要接管鼠标事件。
#pragma once

#include <string>

#include "geometry.h"

namespace ios_screen {

enum class Edge { Right, Left, Top, Bottom };

// 光标离屏边多少像素以内算"贴在边缘带上"。
// 取 2 而不是 0：高 DPI 下 GetCursorPos 与实际命中偶有一像素误差。
constexpr int kEdgeBandPx = 2;

struct EdgeSpec {
  Edge edge = Edge::Right;
  Rect screen;  // 被越界的那块屏，用整屏 rcMonitor，不是 rcWork
};

bool parseEdge(const std::string& s, Edge* out);
const char* edgeName(Edge e);

bool isAtEdge(const EdgeSpec& spec, int x, int y, int bandPx = kEdgeBandPx);

// 原始位移在"朝屏幕外"方向上的分量，朝外为正。
int outwardComponent(const EdgeSpec& spec, int rawDx, int rawDy);

// 朝屏幕内为正，用于退出判定。
int inwardComponent(const EdgeSpec& spec, int rawDx, int rawDy);

struct EdgeTriggerConfig {
  // 朝外累积这么多原始计数才进入。太小会误触（贴边微抖），太大要推很久。
  int enterThreshold = 12;
  // 朝内累积这么多才退出。必须比进入阈值大，否则在边界上会来回抖。
  int exitThreshold = 60;
  // 不朝外时，累积量的衰减速度（每次事件的扣除）。防止"慢慢蹭"攒出误触发。
  int decayPerEvent = 1;
};

// 纯逻辑状态机，不碰 Win32，可以直接写单测。
class EdgeTrigger {
 public:
  enum class Change { None, Entered, Exited };

  explicit EdgeTrigger(EdgeTriggerConfig cfg = {}) : cfg_(cfg) {}

  // rawDx/rawDy 是这一帧的设备级相对位移；cursorX/Y 是当前系统光标位置。
  Change update(const EdgeSpec& spec, int cursorX, int cursorY, int rawDx, int rawDy);

  bool active() const { return active_; }
  int accum() const { return accum_; }
  void reset();

 private:
  EdgeTriggerConfig cfg_;
  bool active_ = false;
  int accum_ = 0;
};

}  // namespace ios_screen
