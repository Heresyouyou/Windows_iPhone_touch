// HID 报文出口。Windows 上由 WinRT HOGP 实现；调试时可以换成打印到控制台的假实现，
// 这样在蓝牙还没通之前就能验证几何、光标、校准整条链路。
#pragma once

#include <cstdint>
#include <cstdio>
#include <string>
#include <vector>

#include "hid_reports.h"

namespace ios_screen {

class HidSink {
 public:
  virtual ~HidSink() = default;
  virtual bool start(std::string* err) = 0;
  virtual void stop() = 0;
  virtual void send(ReportId id, const std::vector<uint8_t>& payload) = 0;
  virtual bool connected() const = 0;
};

// 把报文打印出来，不发蓝牙。用来在没有 HOGP 的情况下验证前半段链路。
class DryRunSink : public HidSink {
 public:
  bool start(std::string* err) override {
    (void)err;
    return true;
  }
  void stop() override {}
  void send(ReportId id, const std::vector<uint8_t>& payload) override {
    std::printf("  report id=%d len=%zu [", static_cast<int>(id), payload.size());
    for (size_t i = 0; i < payload.size(); ++i)
      std::printf(i ? " %02X" : "%02X", payload[i]);
    std::printf("]\n");
  }
  bool connected() const override { return true; }
};

// 由 hogp_winrt.cpp 提供；未实现时返回一个会报错的 sink（不会静默失败）。
HidSink* createHogpSink();

}  // namespace ios_screen
