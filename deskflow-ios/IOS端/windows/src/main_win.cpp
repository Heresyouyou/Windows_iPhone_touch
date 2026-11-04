// Windows 端主程序：把一块虚拟显示器变成"iPhone 的屏幕"。
//
// 与 macOS 版的差别（很重要）：
//   macOS 有画布窗口，所以能"摆窗口 + 隐藏系统指针"，位移交给 iPhone Use 处理；
//   Windows 没有画面，所以本程序自己吃掉该屏上的鼠标事件、自己算位移、自己发 HID。
//
// 本文件在 macOS 上无法编译（Win32 头文件不存在），因此**未经编译验证**。
// 结构、调用顺序、错误处理都写全了，正常应该只需修少量类型/包含问题。
#define NOMINMAX
#include <windows.h>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "calibration.h"
#include "cursor_guard.h"
#include "geometry.h"
#include "hid_reports.h"
#include "hid_sink.h"

namespace {

using ios_screen::Calibration;
using ios_screen::CursorGuard;
using ios_screen::HidSink;
using ios_screen::Rect;

// ---- 配置 ---------------------------------------------------------------

// 退出捕获的余量：在虚拟屏上继续往左推这么多像素就归还控制权。
// 没有这个，"吞掉所有事件"会让用户永远出不来。
constexpr int kExitMarginPx = 60;
// 报文发送间隔，与校准时的 reportInterval 一致；iOS 的加速度按速度分档，改这个要重标。
constexpr int kSendIntervalMs = 30;

struct MonitorRec {
  HMONITOR handle = nullptr;
  RECT bounds{};
  RECT work{};
  std::wstring device;
  bool primary = false;
};

BOOL CALLBACK enumProc(HMONITOR h, HDC, LPRECT, LPARAM param) {
  auto* out = reinterpret_cast<std::vector<MonitorRec>*>(param);
  MONITORINFOEXW mi{};
  mi.cbSize = sizeof(mi);
  if (!GetMonitorInfoW(h, &mi)) return TRUE;
  MonitorRec r;
  r.handle = h;
  r.bounds = mi.rcMonitor;
  r.work = mi.rcWork;
  r.device = mi.szDevice;
  r.primary = (mi.dwFlags & MONITORINFOF_PRIMARY) != 0;
  out->push_back(r);
  return TRUE;
}

std::vector<MonitorRec> enumerateMonitors() {
  std::vector<MonitorRec> list;
  EnumDisplayMonitors(nullptr, nullptr, enumProc, reinterpret_cast<LPARAM>(&list));
  return list;
}

bool contains(const RECT& r, POINT p) {
  return p.x >= r.left && p.x < r.right && p.y >= r.top && p.y < r.bottom;
}

// 把显示器挪到指定原点。不需要写 IddCx 驱动，只要驱动已经存在（Parsec VDD 等）。
bool moveMonitor(const std::wstring& device, int x, int y, RECT* actual) {
  DEVMODEW dm{};
  dm.dmSize = sizeof(dm);
  if (!EnumDisplaySettingsExW(device.c_str(), ENUM_CURRENT_SETTINGS, &dm, 0)) return false;
  dm.dmPosition.x = x;
  dm.dmPosition.y = y;
  dm.dmFields = DM_POSITION;
  const LONG rc = ChangeDisplaySettingsExW(device.c_str(), &dm, nullptr, CDS_UPDATEREGISTRY, nullptr);
  if (rc != DISP_CHANGE_SUCCESSFUL) {
    std::printf("  ChangeDisplaySettingsEx 失败: %ld\n", rc);
    return false;
  }
  // 回读：不要"设完就信"
  DEVMODEW now{};
  now.dmSize = sizeof(now);
  if (EnumDisplaySettingsExW(device.c_str(), ENUM_CURRENT_SETTINGS, &now, 0) && actual) {
    actual->left = now.dmPosition.x;
    actual->top = now.dmPosition.y;
    actual->right = now.dmPosition.x + static_cast<LONG>(now.dmPelsWidth);
    actual->bottom = now.dmPosition.y + static_cast<LONG>(now.dmPelsHeight);
  }
  return true;
}

// ---- 捕获状态 ------------------------------------------------------------

struct Capture {
  bool active = false;
  double desiredX = 0, desiredY = 0;   // 用户想让指针去的地方（iPhone 像素）
  double predictedX = 0, predictedY = 0;  // 按校准曲线推算的当前位置
  POINT lastRaw{};                     // 上一条原始鼠标位置（系统坐标）
  bool haveLast = false;
  int lastDx = 0, lastDy = 0;
  DWORD lastSend = 0;
  double pxPerUnitX = 1.0, pxPerUnitY = 1.0;  // 虚拟屏像素 -> iPhone 像素
};

Capture gCap;
Calibration gCal;
HidSink* gSink = nullptr;
CursorGuard* gCursor = nullptr;
RECT gPhoneRect{};        // 虚拟屏在系统里的矩形（= 手机画面区，盲控模式下整块屏都是）
RECT gPrimaryRect{};      // 主屏矩形，退出捕获时把指针放回这里
HHOOK gHook = nullptr;
bool gVerbose = false;

void sendStep() {
  const auto s = gCal.nextStep(gCap.predictedX, gCap.predictedY, gCap.desiredX, gCap.desiredY,
                               gCap.lastDx, gCap.lastDy);
  if (s.dx == 0 && s.dy == 0) return;
  gSink->send(ios_screen::kRidRelativeMouse, ios_screen::encodeRelativeMouse(0, s.dx, s.dy));
  gCap.predictedX = s.x;
  gCap.predictedY = s.y;
  gCap.lastDx = s.dx;
  gCap.lastDy = s.dy;
}

void beginCapture(POINT raw) {
  gCap.active = true;
  gCap.haveLast = true;
  gCap.lastRaw = raw;
  // 起点按比例映射到手机坐标，避免一进来就跳
  gCap.desiredX = gPhoneRect.right - gPhoneRect.left > 0
                      ? (raw.x - gPhoneRect.left) * gCap.pxPerUnitX
                      : 0;
  gCap.desiredY = gPhoneRect.bottom - gPhoneRect.top > 0
                      ? (raw.y - gPhoneRect.top) * gCap.pxPerUnitY
                      : 0;
  gCap.predictedX = gCap.desiredX;
  gCap.predictedY = gCap.desiredY;
  gCap.lastDx = gCap.lastDy = 0;
  gCursor->enter();
  if (gVerbose) std::printf("  进入捕获 (%.0f,%.0f)\n", gCap.desiredX, gCap.desiredY);
}

void endCapture() {
  if (!gCap.active) return;
  gCap.active = false;
  gCap.haveLast = false;
  gCursor->leave();
  // 把系统指针放回主屏（离右缘留出余量，否则会立刻再次触发进入）
  POINT back{};
  back.x = gPrimaryRect.right - 120;
  back.y = (gPrimaryRect.top + gPrimaryRect.bottom) / 2;
  SetCursorPos(back.x, back.y);
  if (gVerbose) std::printf("  退出捕获\n");
}

LRESULT CALLBACK mouseHook(int code, WPARAM wParam, LPARAM lParam) {
  if (code != HC_ACTION) return CallNextHookEx(gHook, code, wParam, lParam);
  auto* ms = reinterpret_cast<MSLLHOOKSTRUCT*>(lParam);
  // 自己注入的事件必须放行，否则会无限回环
  if (ms->flags & LLMHF_INJECTED) return CallNextHookEx(gHook, code, wParam, lParam);

  const bool inside = contains(gPhoneRect, ms->pt);

  if (!gCap.active) {
    if (inside && wParam == WM_MOUSEMOVE) {
      beginCapture(ms->pt);
      return 1;  // 吞掉，系统指针停在边缘
    }
    return CallNextHookEx(gHook, code, wParam, lParam);
  }

  // 捕获中：只处理移动，按键留给 HID 层（此处先吞掉，避免误触其他窗口）
  if (wParam == WM_MOUSEMOVE) {
    if (!gCap.haveLast) {
      gCap.lastRaw = ms->pt;
      gCap.haveLast = true;
      return 1;
    }
    const int dx = ms->pt.x - gCap.lastRaw.x;
    const int dy = ms->pt.y - gCap.lastRaw.y;
    gCap.lastRaw = ms->pt;
    gCap.desiredX += dx * gCap.pxPerUnitX;
    gCap.desiredY += dy * gCap.pxPerUnitY;

    // 越界就是"想离开"：累积到余量后归还控制权
    const double w = gPhoneRect.right - gPhoneRect.left;
    const double h = gPhoneRect.bottom - gPhoneRect.top;
    if (gCap.desiredX < -kExitMarginPx || gCap.desiredY < -kExitMarginPx ||
        gCap.desiredX > w + kExitMarginPx || gCap.desiredY > h + kExitMarginPx) {
      endCapture();
      return 1;
    }
    gCap.desiredX = std::max(0.0, std::min(w - 1, gCap.desiredX));
    gCap.desiredY = std::max(0.0, std::min(h - 1, gCap.desiredY));

    const DWORD now = GetTickCount();
    if (now - gCap.lastSend >= kSendIntervalMs) {
      gCap.lastSend = now;
      sendStep();
    }
    return 1;
  }
  return 1;  // 捕获期间吞掉滚轮/按键，防止泄漏到桌面
}

BOOL WINAPI consoleHandler(DWORD type) {
  if (type == CTRL_C_EVENT || type == CTRL_CLOSE_EVENT || type == CTRL_BREAK_EVENT) {
    if (gCursor) gCursor->leave();  // 无论如何先恢复指针
    if (gHook) UnhookWindowsHookEx(gHook);
    return TRUE;
  }
  return FALSE;
}

void usage() {
  std::printf(
      "用法: ios_screen --monitor \"<虚拟屏名称片段>\" --calibration <json> [--dry-run] [-v]\n"
      "      ios_screen --list\n"
      "      ios_screen --calibration <json> --home [--dry-run]\n"
      "  --monitor       GetMonitorInfo 里 szDevice 名称，可用 --list 查看\n"
      "  --calibration   Mac 上导出的校准 json（by-screen/<id>.json）\n"
      "  --dry-run       不连蓝牙，把报文打印出来，用于验证几何与校准\n"
      "  --home          只发归位报文（左上角），用来单独验证 HID 能不能推动指针\n");
}

// 归位：连发最大幅度向左再向上，iOS 在边界会钳制，所以之后位置已知。
// 这是唯一不依赖画面就能校准绝对位置的技巧。
int runHoming(HidSink* sink, const Calibration& cal) {
  const auto reports = cal.homingReports();
  std::printf("发送 %zu 条归位报文…\n", reports.size());
  for (const auto& r : reports) {
    sink->send(ios_screen::kRidRelativeMouse, ios_screen::encodeRelativeMouse(0, r.first, r.second));
    Sleep(static_cast<DWORD>(cal.reportInterval * 1000));
  }
  std::printf("归位完成，指针应停在 iPhone 左上角\n");
  return 0;
}

}  // namespace

int main(int argc, char** argv) {
  std::string monitorNeedle, calPath;
  bool dryRun = false, homeMode = false;
  for (int i = 1; i < argc; ++i) {
    const std::string a = argv[i];
    if (a == "--monitor" && i + 1 < argc) monitorNeedle = argv[++i];
    else if (a == "--calibration" && i + 1 < argc) calPath = argv[++i];
    else if (a == "--dry-run") dryRun = true;
    else if (a == "--home") homeMode = true;
    else if (a == "-v") gVerbose = true;
    else if (a == "--list") {
      for (const auto& m : enumerateMonitors()) {
        std::wprintf(L"  %s  %ldx%ld at (%ld,%ld)%s\n", m.device.c_str(),
                     m.bounds.right - m.bounds.left, m.bounds.bottom - m.bounds.top,
                     m.bounds.left, m.bounds.top, m.primary ? L"  [primary]" : L"");
      }
      return 0;
    } else {
      usage();
      return 2;
    }
  }
  if (calPath.empty() || (monitorNeedle.empty() && !homeMode)) {
    usage();
    return 2;
  }

  // 1) 校准曲线
  FILE* f = std::fopen(calPath.c_str(), "rb");
  if (!f) {
    std::printf("读不到校准文件: %s\n", calPath.c_str());
    return 1;
  }
  std::string json;
  char buf[4096];
  size_t n;
  while ((n = std::fread(buf, 1, sizeof(buf), f)) > 0) json.append(buf, n);
  std::fclose(f);
  std::string err;
  gCal = Calibration::fromJson(json, &err);
  if (!gCal.valid(&err)) {
    std::printf("校准文件不可用: %s\n", err.c_str());
    return 1;
  }
  std::printf("校准: %dx%d, %d 档, 节奏 %.0f ms\n", gCal.screenWidth, gCal.screenHeight,
              gCal.maxCount(), gCal.reportInterval * 1000);

  // 归位模式：不碰显示器、不装钩子，只验证 HID 链路
  if (homeMode) {
    ios_screen::DryRunSink dry;
    HidSink* sink = dryRun ? static_cast<HidSink*>(&dry) : ios_screen::createHogpSink();
    if (!sink->start(&err)) {
      std::printf("HID 出口不可用: %s\n", err.c_str());
      return 1;
    }
    const int rc = runHoming(sink, gCal);
    sink->stop();
    return rc;
  }

  // 2) 找到虚拟屏并摆到主屏右侧
  const auto monitors = enumerateMonitors();
  const MonitorRec* target = nullptr;
  for (const auto& m : monitors) {
    if (m.primary) continue;
    char narrow[64] = {0};
    WideCharToMultiByte(CP_UTF8, 0, m.device.c_str(), -1, narrow, sizeof(narrow), nullptr, nullptr);
    if (std::strstr(narrow, monitorNeedle.c_str())) {
      target = &m;
      break;
    }
  }
  if (!target) {
    std::printf("没找到匹配 \"%s\" 的非主显示器，请先装虚拟显示驱动并用 --list 确认名称\n",
                monitorNeedle.c_str());
    return 1;
  }

  RECT primaryRect{};
  for (const auto& m : monitors)
    if (m.primary) primaryRect = m.bounds;
  gPrimaryRect = primaryRect;

  ios_screen::Rect primary{primaryRect.left, primaryRect.top,
                           primaryRect.right - primaryRect.left,
                           primaryRect.bottom - primaryRect.top};
  int wantX = 0, wantY = 0;
  ios_screen::placeMonitorToRight(primary, static_cast<int>(target->bounds.right - target->bounds.left),
                                 static_cast<int>(target->bounds.bottom - target->bounds.top), &wantX,
                                 &wantY);
  RECT actual{};
  if (!moveMonitor(target->device, wantX, wantY, &actual)) {
    std::printf("摆位失败（可能需要在显示设置里先把这块屏启用为扩展）\n");
    return 1;
  }
  std::printf("虚拟屏: %s -> (%ld,%ld) %ldx%ld\n", monitorNeedle.c_str(), actual.left, actual.top,
              actual.right - actual.left, actual.bottom - actual.top);

  // 3) 手机画面区 = 整块虚拟屏（盲控模式没有窗口，所以就是整屏）
  gPhoneRect = actual;
  gCap.pxPerUnitX = static_cast<double>(gCal.screenWidth) / (actual.right - actual.left);
  gCap.pxPerUnitY = static_cast<double>(gCal.screenHeight) / (actual.bottom - actual.top);
  std::printf("像素比: %.2f x %.2f\n", gCap.pxPerUnitX, gCap.pxPerUnitY);

  // 4) 出口：HOGP 或 dry-run
  ios_screen::DryRunSink dry;
  gSink = dryRun ? static_cast<HidSink*>(&dry) : ios_screen::createHogpSink();
  if (!gSink->start(&err)) {
    std::printf("HID 出口不可用: %s\n", err.c_str());
    return 1;
  }

  CursorGuard guard([](bool show) { return ShowCursor(show ? TRUE : FALSE); });
  gCursor = &guard;
  SetConsoleCtrlHandler(consoleHandler, TRUE);

  gHook = SetWindowsHookExW(WH_MOUSE_LL, mouseHook, GetModuleHandleW(nullptr), 0);
  if (!gHook) {
    std::printf("低级鼠标钩子安装失败: %lu\n", GetLastError());
    return 1;
  }
  std::printf("就绪：把鼠标移进虚拟屏即开始控制 iPhone，Ctrl+C 退出\n");

  MSG msg;
  while (GetMessageW(&msg, nullptr, 0, 0) > 0) {
    TranslateMessage(&msg);
    DispatchMessageW(&msg);
  }

  endCapture();
  UnhookWindowsHookEx(gHook);
  gSink->stop();
  return 0;
}
