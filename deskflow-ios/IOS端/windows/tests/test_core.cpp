// 纯逻辑单测：不依赖任何平台 API，macOS 和 Windows 都能跑。
//   cmake -S . -B build && cmake --build build && ./build/test_core
#include <cmath>
#include <cstdio>
#include <string>
#include <vector>

#include "calibration.h"
#include "cursor_guard.h"
#include "expected_report_map.h"
#include "geometry.h"
#include "hid_reports.h"

using namespace ios_screen;

namespace {
int gChecks = 0;
int gFails = 0;

void check(bool ok, const char* what, const std::string& detail = "") {
  ++gChecks;
  if (!ok) {
    ++gFails;
    std::printf("  FAIL  %s  %s\n", what, detail.c_str());
  }
}

void checkNear(double got, double want, double tol, const char* what) {
  char buf[128];
  std::snprintf(buf, sizeof(buf), "got %.6f want %.6f", got, want);
  check(std::fabs(got - want) <= tol, what, buf);
}

// 真机量出来的曲线（iPhone 15 Pro，1180×2556，reportInterval 0.03）
const char* kRealJson = R"({
  "distances": [0, 0.8298611111111112, 1.9294871794871795, 3.3088235294117645,
     4.925, 6.885, 9.05, 11.5625, 14.25, 17.3, 20.625, 24.125, 28.166666666666668,
     32.166666666666664, 36.75, 41.25, 46.25, 51.25, 57.25, 62.75, 68.75, 75.25,
     82.25, 89.25, 96.5, 103.5, 111.5, 119.5, 128.25, 136.25, 145.5, 155.5, 164.5],
  "screenHeight": 2556, "screenWidth": 1180, "reportInterval": 0.03,
  "screenID": "00008130-001858C10883401C"
})";

void testHid() {
  std::printf("[hid] report map / payload\n");
  check(kReportMap.size() == kExpectedReportMapSize, "report map byte count",
        std::to_string(kReportMap.size()) + " vs " + std::to_string(kExpectedReportMapSize));

  // 逐字节与上游 iPhone Use 的 HIDReports.swift 比对（见 expected_report_map.h）。
  // 只要有一位不同，iPhone 侧就可能不显示指针，且必须重新配对才会生效。
  int firstDiff = -1;
  for (size_t i = 0; i < kReportMap.size() && i < kExpectedReportMapSize; ++i)
    if (kReportMap[i] != kExpectedReportMap[i]) { firstDiff = static_cast<int>(i); break; }
  check(firstDiff < 0, "report map is byte-identical to upstream",
        firstDiff < 0 ? "" : "first diff at byte " + std::to_string(firstDiff));

  // 四个 Report ID 各出现一次（0x85 = Report ID 前缀）
  int ids = 0;
  for (size_t i = 0; i + 1 < kReportMap.size(); ++i)
    if (kReportMap[i] == 0x85 && kReportMap[i + 1] >= 1 && kReportMap[i + 1] <= 4) ++ids;
  check(ids == 4, "four report ids in map", std::to_string(ids));

  check(encodeRelativeMouse(0, 0, 0).size() == 4, "relative mouse is 4 bytes");
  check(encodeKeyboard(0, 0).size() == 8, "keyboard is 8 bytes");
  check(encodeAbsolutePointer(0, 0, 0).size() == 5, "absolute pointer is 5 bytes");
  check(encodeConsumer(0x0223).size() == 2, "consumer is 2 bytes");

  // 有符号单字节与钳制
  auto big = encodeRelativeMouse(1, 1000, -1000);
  check(big[1] == 127 && big[2] == 0x81, "clamp to +-127", std::to_string(big[1]) + "," + std::to_string(big[2]));
  check(big[0] == 1, "buttons kept");

  // 拆分：总量必须守恒
  auto parts = encodeRelativeMouseSplit(0, 300, -50);
  int sumX = 0, sumY = 0;
  for (const auto& p : parts) {
    sumX += static_cast<int>(static_cast<int8_t>(p[1]));
    sumY += static_cast<int>(static_cast<int8_t>(p[2]));
    check(std::abs(static_cast<int>(static_cast<int8_t>(p[1]))) <= 127, "chunk within +-127");
  }
  check(sumX == 300 && sumY == -50, "split conserves delta",
        std::to_string(sumX) + "," + std::to_string(sumY));

  // 绝对坐标小端序
  auto abs5 = encodeAbsolutePointer(0, 0x1234, 0x5678);
  check(abs5[1] == 0x34 && abs5[2] == 0x12 && abs5[3] == 0x78 && abs5[4] == 0x56, "absolute is little endian");
}

void testCalibration() {
  std::printf("[calibration] parse / lookup / tracking\n");
  std::string err;
  Calibration c = Calibration::fromJson(kRealJson, &err);
  check(err.empty(), "parse without error", err);
  check(c.valid(&err), "calibration valid", err);
  check(c.distances.size() == 33, "33 entries", std::to_string(c.distances.size()));
  check(c.maxCount() == 32, "max count 32", std::to_string(c.maxCount()));
  check(c.screenWidth == 1180 && c.screenHeight == 2556, "screen size parsed");

  check(c.countForDistance(0.83) == 1, "0.83px -> count 1", std::to_string(c.countForDistance(0.83)));
  check(c.countForDistance(164.5) == 32, "164.5px -> count 32", std::to_string(c.countForDistance(164.5)));
  check(c.countForDistance(30.0) == c.countForDistance(30.0), "stable lookup");

  // 归位：左移 + 上移两条腿，各 ceil(边长/最大步长)+2
  auto homing = c.homingReports();
  const int expect = static_cast<int>(std::ceil(1180.0 / 164.5)) + 2 +
                     static_cast<int>(std::ceil(2556.0 / 164.5)) + 2;
  check(static_cast<int>(homing.size()) == expect, "homing report count",
        std::to_string(homing.size()) + " vs " + std::to_string(expect));
  check(homing.front().first == -32 && homing.front().second == 0, "homing starts with -32 x");

  // 跟踪收敛：从左上角走到右下角，逐步推进
  double x = 0, y = 0;
  int lastDx = 0, lastDy = 0;
  int steps = 0;
  for (; steps < 4000; ++steps) {
    auto s = c.nextStep(x, y, 1179, 2555, lastDx, lastDy);
    if (s.dx == 0 && s.dy == 0) break;
    x = s.x;
    y = s.y;
    lastDx = s.dx;
    lastDy = s.dy;
  }
  checkNear(x, 1179, c.distances[1] / 2 + 1e-6, "converged x");
  checkNear(y, 2555, c.distances[1] / 2 + 1e-6, "converged y");
  std::printf("       收敛用 %d 步\n", steps);

  // 已经在目标上就不该动
  auto still = c.nextStep(500, 500, 500, 500);
  check(still.dx == 0 && still.dy == 0, "no move when on target");
}

void testCursorGuard() {
  std::printf("[cursor] refcount balance\n");
  int counter = 0;   // 模拟系统计数
  int calls = 0;
  CursorGuard g([&](bool show) {
    ++calls;
    counter += show ? 1 : -1;
    return counter;
  });

  g.enter();
  check(counter < 0, "hidden after enter", std::to_string(counter));
  check(g.hideCalls() == 1, "one hide call from zero", std::to_string(g.hideCalls()));
  g.enter();
  check(g.hideCalls() == 1, "enter is idempotent", std::to_string(g.hideCalls()));

  g.leave();
  check(counter == 0, "restored", std::to_string(counter));
  g.leave();
  check(g.showCalls() == 1, "leave is idempotent", std::to_string(g.showCalls()));

  // 别处把计数抬到 3 时，必须循环到负数才真的隐藏
  counter = 3;
  CursorGuard g2([&](bool show) {
    counter += show ? 1 : -1;
    return counter;
  });
  g2.enter();
  check(counter < 0, "hides through positive counter", std::to_string(counter));
  g2.leave();
  check(counter == 0, "back to zero", std::to_string(counter));

  // 析构时仍在隐藏，必须自动恢复
  counter = 0;
  {
    CursorGuard g3([&](bool show) {
      counter += show ? 1 : -1;
      return counter;
    });
    g3.enter();
    check(counter < 0, "g3 hidden");
  }
  check(counter == 0, "destructor restored cursor", std::to_string(counter));
  (void)calls;
}

void testGeometry() {
  std::printf("[geometry] placement matches the verified macOS layout\n");
  Rect primary{0, 0, 1680, 1050};
  int x = 0, y = 0;
  placeMonitorToRight(primary, 509, 905, &x, &y);
  check(x == 1680 && y == 72, "virtual monitor flush right, centered",
        std::to_string(x) + "," + std::to_string(y));

  // 虚拟屏 509x905，去掉 25px 菜单栏后可用区 509x880
  Rect work{1680, 97, 509, 880};
  Rect phone = phoneAreaInWorkArea(work, 393, 852, 28);
  check(phone.x == 1680 && phone.y == 125 && phone.w == 393 && phone.h == 852,
        "phone area is 1680,125 393x852",
        std::to_string(phone.x) + "," + std::to_string(phone.y) + " " +
            std::to_string(phone.w) + "x" + std::to_string(phone.h));
  check(work.contains(phone), "phone area inside work area");

  check(sameRect(phone, Rect{1680, 125, 393, 852}), "sameRect exact");
  check(!sameRect(phone, Rect{1680, 127, 393, 852}), "sameRect rejects 2px drift");
  check(sameRect(phone, Rect{1680, 126, 393, 852}, 1), "sameRect tolerance 1px");
}

}  // namespace

int main() {
  testHid();
  testCalibration();
  testCursorGuard();
  testGeometry();
  std::printf("\n%d checks, %d failed\n", gChecks, gFails);
  return gFails == 0 ? 0 : 1;
}
