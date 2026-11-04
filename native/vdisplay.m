// vdisplay — 创建一块"屏幕外"的虚拟显示器，位置紧贴主屏右侧。
//
// 尺寸默认 393×852 pt（iPhone 15 Pro 点数），垂直居中。
// 进程存活期间存在；退出后自动移除，桌面恢复原状。
//
// 为什么要它
// ----------
// Deskflow 这类远程键鼠工具，在 client 侧维护的是**自己的**光标坐标，
// 上限就是上报给 server 的桌面宽度。桌面只有 1680 宽时，光标顶到 1680
// 就没有空间了，它合成的 DeltaX 会直接变成 0——于是 iPhone 上的指针
// 只能沿左缘上下滑（Y 没到边界所以正常）。
//
// 右侧多出这块屏之后，桌面宽度变成 1680+393，光标能真正走进来，
// DeltaX 不再归零。而且 393×852 正好是 iPhone 的点数，坐标天然 1:1。

#import <Cocoa/Cocoa.h>
#import <CoreGraphics/CoreGraphics.h>
#import <signal.h>
#import <unistd.h>

@interface CGVirtualDisplayDescriptor : NSObject
@property (nonatomic, copy) NSString *name;
@property (nonatomic) uint32_t maxPixelsWide, maxPixelsHigh;
@property (nonatomic) CGSize sizeInMillimeters;
@property (nonatomic) uint32_t productID, vendorID, serialNum;
@property (nonatomic, strong) dispatch_queue_t queue;
@property (nonatomic, copy) void (^terminationHandler)(id, id);
@end

@interface CGVirtualDisplayMode : NSObject
- (instancetype)initWithWidth:(uint32_t)w height:(uint32_t)h refreshRate:(double)r;
@end

@interface CGVirtualDisplaySettings : NSObject
@property (nonatomic) uint32_t hiDPI;
@property (nonatomic, copy) NSArray *modes;
@end

@interface CGVirtualDisplay : NSObject
- (instancetype)initWithDescriptor:(CGVirtualDisplayDescriptor *)d;
- (BOOL)applySettings:(CGVirtualDisplaySettings *)s;
@property (nonatomic, readonly) CGDirectDisplayID displayID;
@end

static CGDirectDisplayID builtinID;
static CGDirectDisplayID virtID;
static CGRect builtinBounds;
static double ptW = 393, ptH = 852;   // iPhone 15 Pro 的点数
static pid_t parentPid = 0;           // 非 0 时盯着它：父进程没了就退出

static void handler(int sig) { _exit(0); }

/// 父进程还活着吗。留一块无人管理的虚拟屏会给用户留下一块永远黑着的
/// 显示器，还会一直占着桌面宽度，所以这里必须看住。
static int parentAlive(void) {
    if (parentPid <= 0) return 1;
    return kill(parentPid, 0) == 0 || errno == EPERM;
}

/// 找到与内置屏原始分辨率对应的模式（含 HiDPI 缩放模式）
static CGDisplayModeRef copyBuiltinMode(void) {
    NSDictionary *opts = @{ (__bridge NSString *)kCGDisplayShowDuplicateLowResolutionModes: @YES };
    CFArrayRef modes = CGDisplayCopyAllDisplayModes(builtinID, (__bridge CFDictionaryRef)opts);
    if (!modes) return NULL;
    CGDisplayModeRef found = NULL;
    for (id m in (__bridge_transfer NSArray *)modes) {
        CGDisplayModeRef mm = (__bridge CGDisplayModeRef)m;
        if (CGDisplayModeGetWidth(mm) == builtinBounds.size.width &&
            CGDisplayModeGetHeight(mm) == builtinBounds.size.height) {
            found = (CGDisplayModeRef)CFRetain((CFTypeRef)mm);
            break;
        }
    }
    return found;
}

static BOOL builtinModeMatches(void) {
    CGDisplayModeRef cur = CGDisplayCopyDisplayMode(builtinID);
    if (!cur) return YES;
    BOOL same = (CGDisplayModeGetWidth(cur) == builtinBounds.size.width &&
                 CGDisplayModeGetHeight(cur) == builtinBounds.size.height);
    CFRelease(cur);
    return same;
}

static void restoreBuiltinMode(void) {
    if (builtinModeMatches()) return;
    CGDisplayModeRef m = copyBuiltinMode();
    if (m) {
        CGDisplaySetDisplayMode(builtinID, m, NULL);
        CFRelease(m);
    }
}

/// 内置屏固定在 (0,0) 作主屏，虚拟屏放到其右侧、垂直居中
static BOOL arrange(void) {
    if (!virtID) return NO;
    double x = builtinBounds.size.width;
    double y = (builtinBounds.size.height - ptH) / 2.0;
    CGDisplayConfigRef cfg;
    if (CGBeginDisplayConfiguration(&cfg) != kCGErrorSuccess) return NO;
    CGConfigureDisplayOrigin(cfg, builtinID, 0, 0);
    CGConfigureDisplayMirrorOfDisplay(cfg, virtID, kCGNullDirectDisplay);
    CGConfigureDisplayOrigin(cfg, virtID, (int)x, (int)y);
    return CGCompleteDisplayConfiguration(cfg, kCGConfigureForSession) == kCGErrorSuccess;
}

int main(int argc, const char **argv) {
    @autoreleasepool {
        setvbuf(stdout, NULL, _IOLBF, 0);
        NSString *name = @"iPhone Virtual";
        for (int i = 1; i < argc; i++) {
            NSString *a = [NSString stringWithUTF8String:argv[i]];
            if ([a isEqualToString:@"--name"] && i + 1 < argc) name = [NSString stringWithUTF8String:argv[++i]];
            else if ([a isEqualToString:@"--pt-w"] && i + 1 < argc) ptW = atof(argv[++i]);
            else if ([a isEqualToString:@"--pt-h"] && i + 1 < argc) ptH = atof(argv[++i]);
            else if ([a isEqualToString:@"--parent-pid"] && i + 1 < argc) parentPid = (pid_t)atoi(argv[++i]);
        }

        builtinID = CGMainDisplayID();
        builtinBounds = CGDisplayBounds(builtinID);
        signal(SIGTERM, handler);
        signal(SIGINT, handler);

        CGVirtualDisplayDescriptor *d = [[CGVirtualDisplayDescriptor alloc] init];
        d.name = name;
        d.maxPixelsWide = (uint32_t)(ptW * 2);
        d.maxPixelsHigh = (uint32_t)(ptH * 2);
        d.sizeInMillimeters = CGSizeMake(600.0, 600.0 * ptH / ptW);
        d.productID = 0x1D15; d.vendorID = 0x4B55; d.serialNum = 1;
        d.queue = dispatch_get_main_queue();

        CGVirtualDisplay *vd = [[CGVirtualDisplay alloc] initWithDescriptor:d];
        if (!vd) { fprintf(stderr, "创建虚拟显示器失败\n"); return 1; }
        CGVirtualDisplaySettings *s = [[CGVirtualDisplaySettings alloc] init];
        s.hiDPI = 1;
        s.modes = @[[[CGVirtualDisplayMode alloc] initWithWidth:(uint32_t)ptW
                                                        height:(uint32_t)ptH
                                                   refreshRate:60]];
        if (![vd applySettings:s]) { fprintf(stderr, "应用显示模式失败\n"); return 1; }
        virtID = vd.displayID;

        for (int i = 0; i < 30 && !CGDisplayIsActive(virtID); i++) usleep(100000);
        sleep(2);                       // 等系统首次配置落定

        arrange();                      // 先摆位置：内置屏回 (0,0)，虚拟屏靠右
        usleep(800000);
        restoreBuiltinMode();           // 再恢复内置屏分辨率（会被重置成 1440×900）
        usleep(800000);
        arrange();                      // 分辨率变更可能挪动排列，再摆一次

        printf("virtual: id=%u main=%d; builtin main=%d mode_ok=%d\n",
               virtID, CGDisplayIsMain(virtID), CGDisplayIsMain(builtinID), builtinModeMatches());

        time_t lastFix = 0;
        while (1) {
            sleep(1);
            if (!parentAlive()) _exit(0);      // 主程序没了就带着虚拟屏一起走
            if (!builtinModeMatches() && time(NULL) - lastFix > 2) {
                restoreBuiltinMode();
                usleep(600000);
                arrange();
                lastFix = time(NULL);
            }
            CGRect vb = CGDisplayBounds(virtID);
            double ex = builtinBounds.size.width;
            double ey = (builtinBounds.size.height - ptH) / 2.0;
            if (CGDisplayIsActive(virtID) && (fabs(vb.origin.x - ex) > 1 || fabs(vb.origin.y - ey) > 1)) {
                if (time(NULL) - lastFix > 2) { arrange(); lastFix = time(NULL); }
            }
        }
    }
    return 0;
}
