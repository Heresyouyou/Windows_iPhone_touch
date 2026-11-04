# iPhone Use 本地控制协议

iPhone Use 在 `127.0.0.1:7390` 起了一个 HTTP 服务，本项目的所有动作都通过它转发。**接口非官方、无文档、随版本可能变化**，下面的内容是按实测整理的，仅作为集成参考。

## 端点

| 方法 | 路径 | 用途 | 实测 |
|---|---|---|---|
| GET | `/status` | 全量状态（蓝牙、采集、指针、校准） | 200 |
| GET | `/devices` | 设备精简列表 | 200 |
| GET | `/config` | 应用配置 | 200 |
| GET | `/screenshot` | 当前画面 JPEG（约 50 KB） | 200 |
| POST | `/move` | 绝对定位，参数 `{"x":0..1,"y":0..1}` | 仅 POST |
| POST | `/nudge` | 相对位移 `{"dx":..,"dy":..}` | 仅 POST |
| POST | `/tap` | 单击 |
| POST | `/calibration/auto` | 自动校准（32 档） |
| POST | `/calibration/reload` | 重新加载校准曲线 |

`GET` 一个只接受 `POST` 的路径会返回 404 而不是 405，所以探测时别用 GET 的存在性判断。

## /status 结构

```json
{
  "bluetooth": "1 connected, advertising as iPhone Use",
  "bluetoothHosts": [{
    "host": "504E4381-…",
    "screen": { "id": "00008130-001858C10883401C", "name": "KMUST-5G" },
    "session": "5A77EDE4-…",
    "subscribed": ["absolutePointer", "battery", "consumer", "keyboard", "relativeMouse"]
  }],
  "capture": {
    "identityState": "singleDevice",
    "permitsPointerAssociation": true,
    "screens": [{ "identityVerified": false, "physicalID": "…", "sourceID": "…" }]
  },
  "devices": [{
    "id": "00008130-001858C10883401C",
    "name": "KMUST-5G",
    "width": 1180, "height": 2556,
    "bluetooth": true,
    "bluetoothState": "connected",
    "pointer": {
      "ready": true,
      "needsCalibration": false,
      "calibration": {
        "mode": "…", "host": "…", "loaded": true, "filePresent": true,
        "screenID": "…", "screenMatches": true,
        "screenWidth": 1180, "screenHeight": 2556,
        "calibratedWidth": 1180, "calibratedHeight": 2556,
        "reportInterval": "…", "distances": [ … ]
      }
    }
  }]
}
```

字段含义：

- `bluetoothHosts[].subscribed` 是 iPhone 实际订阅的 HID 报告类型。**缺 `relativeMouse` 就说明鼠标没被识别**，这时 `/move` 无效——本项目的越界控制依赖 `relativeMouse`；
- `pointer.ready` 为真才代表校准可用。`needsCalibration` 为真时 iPhone 上会提示"需要指针校准"；
- `calibration.screenMatches` 为假，说明校准是按另一块屏（另一台 Mac）做的，坐标系对不上，必须在本机重新校准；
- `calibration.distances` 是 32 档的位移表，**不要在运行时改它**。

## 校准

```bash
curl -s -X POST http://127.0.0.1:7390/calibration/auto
```

失败的典型原因不是接口问题，而是环境：

| 现象 | 原因 |
|---|---|
| 0/32 档失败 | 画面在动。iPhone Use 要求相邻帧差异 ≤ 256 像素 |
| 只有某一档失败 | 该档的落点上有文字/图标/动效，被判定为画面变化 |
| 全部通过但指针偏 | 校准正确、坐标系原点不同。不要重标，调几何 |

所以校准前请：切到纯白界面 → 把光标移出可能高亮的区域 → 再跑。

校准文件：

```
~/Library/Application Support/iPhoneUse/pointer-calibration/by-screen/<screenID>.json
```

按屏幕分别存储（`screenID` 是 Mac 显示器的 ID），换屏或改分辨率后要重标。曲线范围 0.83 → 164.5 px/档。

## 截图限制

`/screenshot` 返回的 JPEG 只有 49 KB 左右，如果拿到的是纯色块，不是采集坏了——iPhone Use 的窗口 `sharingState = 0`，窗口内容不允许被屏幕录制捕获，这是它的隐私设置。**本项目的键鼠链路完全不依赖截图。**

## 版本兼容

接口没有任何版本号。升级 iPhone Use 之后如果本程序异常，先跑：

```bash
curl -s http://127.0.0.1:7390/status | python3 -m json.tool | head -30
```

确认 `pointer.ready`、`subscribed` 两个字段还在，再判断是不是接口变了。
