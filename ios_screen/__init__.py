"""Mac iPhone Touch：把 iPhone 当作屏幕边缘外的一块屏（macOS 盲控版）。

模块分工：
    hid            198 字节 Report Map 与报文编码（与上游逐字节一致）
    gatt_table     HOGP 的 GATT 数据库，纯数据，可单测
    ble_hid        CoreBluetooth 外设（CBPeripheralManager）
    mac_input      CGEventTap / 光标 / 显示器
    calibration    32 档加速度曲线
    vpointer       虚拟绝对坐标与推拉退出
    edge_trigger   边缘状态机
    keymap         macOS 键码 → HID usage + 按键状态机
    core           Sender / Controller，两种模式共用
    app            边缘模式入口
    panel          面板模式入口
"""
