#include "cursor_guard.h"

namespace ios_screen {
namespace {
constexpr int kMaxCalls = 128;  // 计数被别的程序改乱时的保险丝
}

CursorGuard::CursorGuard(CounterFn fn) : fn_(std::move(fn)) {}

CursorGuard::~CursorGuard() { leave(); }

void CursorGuard::enter() {
  if (hidden_ || !fn_) return;
  for (int i = 0; i < kMaxCalls; ++i) {
    lastCounter_ = fn_(false);
    ++hideCalls_;
    if (lastCounter_ < 0) break;
  }
  hidden_ = true;
}

void CursorGuard::leave() {
  if (!hidden_ || !fn_) return;
  for (int i = 0; i < kMaxCalls; ++i) {
    lastCounter_ = fn_(true);
    ++showCalls_;
    if (lastCounter_ >= 0) break;
  }
  hidden_ = false;
}

}  // namespace ios_screen
