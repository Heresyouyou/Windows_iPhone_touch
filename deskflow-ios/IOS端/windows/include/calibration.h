// 指针校准曲线 —— iOS 的相对指针有加速度，位移与报文幅度不是线性关系。
// distances[i] = 发送 1 条幅度为 i 的 report，iPhone 指针实际移动的物理像素。
// 下标 0 是静止报文（0 像素），i 从 1 到 32。
//
// 这张表是 iOS 侧的行为，与主机无关：同一台 iPhone 上量出来的表，换一台电脑
// 仍然适用。Windows 端没有画面、无法自测，直接沿用 Mac 上量的这份即可。
#pragma once

#include <string>
#include <utility>
#include <vector>

namespace ios_screen {

struct Calibration {
  std::vector<double> distances;  // distances[0] == 0，严格递增
  int screenWidth = 0;            // iPhone 物理像素
  int screenHeight = 0;
  double reportInterval = 0.03;   // 两条 report 之间的间隔（秒），决定加速度所处的速度段

  // 从 /status 的 calibration 片段或校准文件解析；失败时 err 非空。
  static Calibration fromJson(const std::string& text, std::string* err = nullptr);

  bool valid(std::string* err = nullptr) const;

  int maxCount() const { return static_cast<int>(distances.size()) - 1; }

  // 反向查表：想移动 px 像素，最接近的档位（至少 1，最多 maxCount）。
  int countForDistance(double px) const;

  struct Step {
    int dx = 0, dy = 0;     // 下一条 report 的分量（未发送时为 0）
    double x = 0, y = 0;    // 发送后预测的指针位置（物理像素）
  };

  // 单步跟踪：从 (x,y) 走向 (targetX,targetY)。lastDx/lastDy 传上一条 report，
  // 用于交替轴（避免一直用同一轴导致某方向饥饿）。返回 dx==0&&dy==0 表示已到位。
  Step nextStep(double x, double y, double targetX, double targetY,
                int lastDx = 0, int lastDy = 0) const;

  // 归位：连发最大幅度向左，再向上。iOS 在屏幕边界会钳制，所以多给两条余量，
  // 之后就可以从 (0,0) 开始用相对位移推算绝对位置。
  std::vector<std::pair<int, int>> homingReports() const;
};

}  // namespace ios_screen
