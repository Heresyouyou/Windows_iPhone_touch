// HOGP（HID over GATT）发布端。
//
// 这个文件是**骨架**，不是实现：WinRT 的 GATT 服务端 API 必须在本机编译、
// 在本机蓝牙适配器上跑过才有意义，写一份"看起来完整但没跑过"的代码反而会
// 误导后来的人。所以这里只做三件事：
//   1. 把调用顺序和每个参数写清楚（照抄即可）；
//   2. 保留失败路径的判定方法（哪一步抛异常 = 什么原因）；
//   3. 提供一个明确的"未实现"出口，绝不假装成功。
//
// 填写步骤见 IOS端/docs/06-windows-handoff.md 第 4 节。
#include <cstdio>
#include <string>

#include "hid_sink.h"

#if defined(_WIN32) && defined(IOS_SCREEN_WITH_WINRT)

// ---- C++/WinRT 版本：按下面的顺序实现，每一步都能单独验证 ----------------
//
// #include <winrt/Windows.Devices.Bluetooth.GenericAttributeProfile.h>
// #include <winrt/Windows.Devices.Bluetooth.h>
// #include <winrt/Windows.Storage.Streams.h>
// using namespace winrt;
// using namespace Windows::Devices::Bluetooth::GenericAttributeProfile;
//
// 步骤 1：创建服务（UUID 用 Bluetooth SIG 基础 UUID + 0x1812）
//   auto params = GattServiceProviderAdvertisingParameters();
//   params.IsConnectable(true);
//   params.IsDiscoverable(true);
//   auto res = GattServiceProvider::CreateAsync(guid("{00001812-0000-1000-8000-00805F9B34FB}")).get();
//   if (res.Error() != BluetoothError::Success) -> 适配器不支持外设角色
//
// 步骤 2：加 7 个特征（属性/权限见 docs/06 表格）
//   GattLocalCharacteristicParameters p;
//   p.CharacteristicProperties(GattCharacteristicProperties::Read | ...);
//   p.ReadProtectionLevel(GattProtectionLevel::Plain);
//   p.StaticValue(...);            // 0x2A4A/0x2A4B/0x2A4E 用静态值
//   p.Descriptors().Append(...)    // 0x2A4D 必须挂 Report Reference 描述符
//   auto c = res.Service().CreateCharacteristicAsync(uuid, p).get();
//
// 步骤 3：开始广播，然后用 iPhone 设置→蓝牙配对
//   auto adv = provider.StartAdvertising(params);
//   if (adv.Error() != BluetoothError::Success) -> 原因同上
//
// 步骤 4：发送 = 对 Report 特征调用 NotifyValueAsync(IBuffer)
//   writeBytes(0x2A4D + reportId, payload)  // 负载不含 report id 字节
//
// 判定表：
//   抛 hresult_error / BluetoothError::RadioNotAvailable   -> 没有蓝牙
//   BluetoothError::ResourceInUse                           -> 已被别的程序占用
//   StartAdvertising 返回 OtherError                        -> 适配器不支持 Peripheral
//   配对成功但 iPhone 不显示指针                            -> Report Map 或 Report Reference 写错

#endif  // _WIN32 && IOS_SCREEN_WITH_WINRT

namespace ios_screen {
namespace {

class UnimplementedHogpSink : public HidSink {
 public:
  bool start(std::string* err) override {
    if (err) {
      *err =
          "HOGP 尚未实现。请先按 docs/06-windows-handoff.md 第 4 节填写 "
          "hogp_winrt.cpp；在此之前用 --dry-run 验证几何与校准链路。";
    }
    return false;
  }
  void stop() override {}
  void send(ReportId, const std::vector<uint8_t>&) override {
    std::fprintf(stderr, "HOGP 未实现，报文丢弃\n");
  }
  bool connected() const override { return false; }
};

}  // namespace

HidSink* createHogpSink() {
  static UnimplementedHogpSink sink;
  return &sink;
}

}  // namespace ios_screen
