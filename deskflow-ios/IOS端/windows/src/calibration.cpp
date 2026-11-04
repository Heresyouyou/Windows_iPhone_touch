#include "calibration.h"

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <cstring>

namespace ios_screen {
namespace {

// 极简扫描：只在 JSON 里定位 "key": 后面的数字或数组。够用且零依赖。
const char* findKey(const std::string& s, const std::string& key) {
  const std::string needle = "\"" + key + "\"";
  size_t p = s.find(needle);
  if (p == std::string::npos) return nullptr;
  p += needle.size();
  while (p < s.size() && (s[p] == ' ' || s[p] == ':' || s[p] == '\t' || s[p] == '\n' || s[p] == '\r')) ++p;
  return s.c_str() + p;
}

bool readNumber(const std::string& s, const std::string& key, double* out) {
  const char* p = findKey(s, key);
  if (!p) return false;
  char* end = nullptr;
  const double v = std::strtod(p, &end);
  if (end == p) return false;
  *out = v;
  return true;
}

bool readNumberArray(const std::string& s, const std::string& key, std::vector<double>* out) {
  const char* p = findKey(s, key);
  if (!p || *p != '[') return false;
  ++p;
  out->clear();
  while (*p && *p != ']') {
    char* end = nullptr;
    const double v = std::strtod(p, &end);
    if (end == p) {
      if (*p == ',') { ++p; continue; }
      return false;
    }
    out->push_back(v);
    p = end;
    while (*p == ' ' || *p == ',' || *p == '\n' || *p == '\r' || *p == '\t') ++p;
  }
  return *p == ']';
}

}  // namespace

Calibration Calibration::fromJson(const std::string& text, std::string* err) {
  Calibration c;
  if (!readNumberArray(text, "distances", &c.distances)) {
    if (err) *err = "missing or malformed \"distances\"";
    return c;
  }
  double w = 0, h = 0;
  if (!readNumber(text, "screenWidth", &w) || !readNumber(text, "screenHeight", &h)) {
    if (err) *err = "missing screenWidth/screenHeight";
    return c;
  }
  c.screenWidth = static_cast<int>(w);
  c.screenHeight = static_cast<int>(h);
  double interval = 0.0;
  if (readNumber(text, "reportInterval", &interval) && interval > 0) c.reportInterval = interval;
  return c;
}

bool Calibration::valid(std::string* err) const {
  auto fail = [&](const char* m) {
    if (err) *err = m;
    return false;
  };
  if (screenWidth <= 0 || screenWidth > 16384 || screenHeight <= 0 || screenHeight > 16384)
    return fail("bad screen size");
  if (!(reportInterval >= 0.01 && reportInterval <= 0.1)) return fail("bad reportInterval");
  if (distances.size() < 2 || distances.size() > 128) return fail("bad distances count");
  if (distances[0] != 0.0) return fail("distances[0] must be 0");
  for (size_t i = 0; i < distances.size(); ++i) {
    if (!std::isfinite(distances[i]) || distances[i] < 0) return fail("non-finite distance");
    if (i > 0 && !(distances[i] > distances[i - 1])) return fail("distances not increasing");
  }
  if (distances[1] < 0.05) return fail("first step too small");
  const double last = distances.back();
  if (std::ceil(screenWidth / last) + std::ceil(screenHeight / last) > 512)
    return fail("homing would exceed 512 reports");
  return true;
}

int Calibration::countForDistance(double px) const {
  if (distances.size() < 2) return 0;
  const double want = std::fabs(px);
  int best = 1;
  double bestErr = std::fabs(distances[1] - want);
  // 严格小于：距离相同时取更小的档位（与原实现的 min 语义一致）
  for (int i = 2; i <= maxCount(); ++i) {
    const double e = std::fabs(distances[i] - want);
    if (e < bestErr) { bestErr = e; best = i; }
  }
  return best;
}

Calibration::Step Calibration::nextStep(double x, double y, double targetX, double targetY,
                                        int lastDx, int /*lastDy*/) const {
  Step out;
  out.x = x;
  out.y = y;
  if (distances.size() < 2) return out;

  const double tx = std::max(0.0, std::min(static_cast<double>(screenWidth - 1), targetX));
  const double ty = std::max(0.0, std::min(static_cast<double>(screenHeight - 1), targetY));
  const double remaining[2] = {tx - x, ty - y};
  const double largest = std::max(std::fabs(remaining[0]), std::fabs(remaining[1]));
  const double tolerance = distances[1] / 2.0;
  if (largest <= tolerance) return out;   // 已到位

  int counts[2] = {0, 0};
  for (int axis = 0; axis < 2; ++axis) {
    const double err = std::fabs(remaining[axis]);
    if (err <= tolerance) continue;
    // 不要越过目标：单步不超过剩余量按比例缩放后的长度
    const double distance = std::min(err, distances.back() * err / largest);
    int pick = 0;
    for (int i = 1; i <= maxCount(); ++i)
      if (distances[i] <= distance + tolerance) pick = i;
    counts[axis] = pick;
  }

  // 上一轴发了就换另一轴，避免大目标时某一轴一直饿着
  const int preferred = (lastDx != 0) ? 1 : ((std::fabs(remaining[0]) >= std::fabs(remaining[1])) ? 0 : 1);
  for (int k = 0; k < 2; ++k) {
    const int axis = (k == 0) ? preferred : 1 - preferred;
    if (counts[axis] <= 0) continue;
    const double sign = (remaining[axis] < 0) ? -1.0 : 1.0;
    const double delta = sign * distances[counts[axis]];
    if (axis == 0) {
      out.dx = static_cast<int>(sign) * counts[0];
      out.x = std::max(0.0, std::min(static_cast<double>(screenWidth - 1), x + delta));
    } else {
      out.dy = static_cast<int>(sign) * counts[1];
      out.y = std::max(0.0, std::min(static_cast<double>(screenHeight - 1), y + delta));
    }
    return out;
  }
  return out;
}

std::vector<std::pair<int, int>> Calibration::homingReports() const {
  std::vector<std::pair<int, int>> out;
  if (distances.size() < 2) return out;
  const int count = maxCount();
  const double step = distances.back();
  const int nx = static_cast<int>(std::ceil(screenWidth / step)) + 2;
  const int ny = static_cast<int>(std::ceil(screenHeight / step)) + 2;
  out.reserve(static_cast<size_t>(nx + ny));
  for (int i = 0; i < nx; ++i) out.emplace_back(-count, 0);
  for (int i = 0; i < ny; ++i) out.emplace_back(0, -count);
  return out;
}

}  // namespace ios_screen
