"""画一个启动台图标，输出 native/launcher.icns。

用 AppKit 画而不是找现成素材：这个项目没有任何二进制资源依赖，
图标也应当能用代码重新生成。运行一次即可，产物已经在仓库里。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

import AppKit

SIZE = 1024
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "native", "launcher.icns")


def srgb(r, g, b, a=1.0):
    return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(
        r / 255.0, g / 255.0, b / 255.0, a
    )


def rounded(x, y, w, h, r):
    return AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        AppKit.NSMakeRect(x, y, w, h), r, r
    )


def draw() -> None:
    # 底：深蓝渐变圆角方，留出 macOS 图标惯有的 100px 边距
    base = rounded(100, 100, 824, 824, 185)
    grad = AppKit.NSGradient.alloc().initWithStartingColor_endingColor_(
        srgb(58, 78, 112), srgb(26, 33, 48)
    )
    grad.drawInBezierPath_angle_(base, -90)

    # 左：显示器
    srgb(255, 255, 255, 0.96).setFill()
    rounded(205, 395, 350, 250, 26).fill()
    srgb(30, 42, 64).setFill()
    rounded(231, 421, 298, 184, 14).fill()

    # 右：手机
    srgb(255, 255, 255, 0.96).setFill()
    rounded(600, 330, 200, 380, 38).fill()
    srgb(30, 42, 64).setFill()
    rounded(620, 372, 160, 296, 22).fill()

    # 中间一条连接线，正中压一个绿点
    srgb(255, 255, 255, 0.9).setFill()
    AppKit.NSBezierPath.fillRect_(AppKit.NSMakeRect(548, 503, 60, 12))
    srgb(52, 211, 153).setFill()
    AppKit.NSBezierPath.bezierPathWithOvalInRect_(
        AppKit.NSMakeRect(551, 486, 46, 46)
    ).fill()


def render(path: str) -> None:
    rep = AppKit.NSBitmapImageRep.alloc() \
        .initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
            None, SIZE, SIZE, 8, 4, True, False,
            AppKit.NSDeviceRGBColorSpace, 0, 0)
    ctx = AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    AppKit.NSGraphicsContext.saveGraphicsState()
    AppKit.NSGraphicsContext.setCurrentContext_(ctx)
    AppKit.NSColor.clearColor().setFill()
    AppKit.NSBezierPath.fillRect_(AppKit.NSMakeRect(0, 0, SIZE, SIZE))
    draw()
    ctx.flushGraphics()
    AppKit.NSGraphicsContext.restoreGraphicsState()

    data = rep.representationUsingType_properties_(
        AppKit.NSBitmapImageFileTypePNG, None)
    data.writeToFile_atomically_(path, True)


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # iconset 只是 iconutil 的中间产物，放进临时目录，别污染仓库。
    # 目录名必须以 .iconset 结尾，iconutil 靠这个认格式。
    tmp = tempfile.mkdtemp(prefix="launcher-icon-")
    iconset = os.path.join(tmp, "launcher.iconset")
    os.makedirs(iconset)

    try:
        big = os.path.join(iconset, "icon_512x512@2x.png")
        render(big)

        # iconutil 只认这一套固定尺寸
        pairs = [
            ("16x16", 16), ("16x16@2x", 32),
            ("32x32", 32), ("32x32@2x", 64),
            ("128x128", 128), ("128x128@2x", 256),
            ("256x256", 256), ("256x256@2x", 512),
            ("512x512", 512),
        ]
        for name, px in pairs:
            dst = os.path.join(iconset, f"icon_{name}.png")
            subprocess.run(["sips", "-z", str(px), str(px), big, "--out", dst],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=True)

        subprocess.run(["iconutil", "-c", "icns", iconset, "-o", OUT], check=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("已生成", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
