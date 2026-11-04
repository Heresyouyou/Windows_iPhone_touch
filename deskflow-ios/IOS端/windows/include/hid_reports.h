// HID 报文与描述符 —— 与 iPhone Use 的 HIDReports.swift 逐字节一致。
//
// 为什么必须一致：iPhone 会把 HOGP 服务的数据库缓存进配对记录。Report ID、
// 描述符顺序、每个报文的字节数只要有一处不同，iPhone 侧的指针行为就会变，
// 甚至完全不显示指针。改这里 = 必须在 iPhone 上"忽略此设备"重新配对。
#pragma once

#include <cstdint>
#include <vector>

namespace ios_screen {

// Report ID 定义在 Report Reference 描述符（0x2908）里，所以通知负载不带 ID 字节。
enum ReportId : uint8_t {
  kRidKeyboard = 1,
  kRidAbsolutePointer = 2,
  kRidRelativeMouse = 3,
  kRidConsumer = 4,
};

// 绝对指针 X/Y 的最大值（15 位有效）。
constexpr int kAbsoluteMax = 32767;

// 完整 Report Map：keyboard + absolutePointer + relativeMouse + consumer。
// 顺序不要调整，iOS 按顺序解析。
extern const std::vector<uint8_t> kReportMap;

// 8 字节：修饰键、保留、6 个键码。
std::vector<uint8_t> encodeKeyboard(uint8_t modifiers, uint8_t usage);

// 4 字节：按键、dx、dy、滚轮（后三者是有符号单字节）。
std::vector<uint8_t> encodeRelativeMouse(uint8_t buttons, int dx, int dy);

// 5 字节：按键、X(LE u16)、Y(LE u16)。
std::vector<uint8_t> encodeAbsolutePointer(uint8_t buttons, uint16_t x, uint16_t y);

// 2 字节：一个 consumer usage（Home = 0x0223）。
std::vector<uint8_t> encodeConsumer(uint16_t usage);

// 把超过 ±127 的位移拆成多条 report，总量不变（单条 u8 无法表达）。
std::vector<std::vector<uint8_t>> encodeRelativeMouseSplit(uint8_t buttons, int dx, int dy);

}  // namespace ios_screen
