// 光标隐藏/恢复。
//
// Windows 的 ShowCursor 是引用计数，不是布尔开关：返回的是调用后的计数值，
// 只有计数 < 0 才真正隐藏。直接 ShowCursor(FALSE) 一次经常"没反应"，因为
// 别处已经把它加回 0 以上了。所以必须循环到负数，恢复时同样循环到 >= 0。
//
// 这个类把"计数操作"抽象成回调，好处是可以在没有 Win32 的机器上做单元测试，
// 也方便注入假的计数器验证配平。
#pragma once

#include <functional>

namespace ios_screen {

class CursorGuard {
 public:
  // show=true 对应 ShowCursor(TRUE)，返回调用后的计数。
  using CounterFn = std::function<int(bool show)>;

  explicit CursorGuard(CounterFn fn);
  ~CursorGuard();

  CursorGuard(const CursorGuard&) = delete;
  CursorGuard& operator=(const CursorGuard&) = delete;

  // 隐藏；重复调用无副作用。
  void enter();
  // 恢复；重复调用无副作用。析构时若仍处于隐藏状态会自动恢复。
  void leave();

  bool hidden() const { return hidden_; }
  int lastCounter() const { return lastCounter_; }
  // 隐藏/恢复各调了几次 ShowCursor，用于排查"别处也在改计数"的情况。
  int hideCalls() const { return hideCalls_; }
  int showCalls() const { return showCalls_; }

 private:
  CounterFn fn_;
  bool hidden_ = false;
  int lastCounter_ = 0;
  int hideCalls_ = 0;
  int showCalls_ = 0;
};

}  // namespace ios_screen
