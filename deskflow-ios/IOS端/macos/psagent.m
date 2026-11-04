// psagent — iPhone Use 副屏守护进程
//
// 行为：
//   检测到 iPhone Use 启动 → 建虚拟屏(509×905) → 激活窗口 → 摆到虚拟屏 → 光标接管
//   App 退出              → 释放虚拟屏，桌面恢复原状
//
// 单个进程承载全部状态，杀掉它就等于完全撤销。

#import <Cocoa/Cocoa.h>
#import <CoreGraphics/CoreGraphics.h>
#import <signal.h>
#import <libproc.h>
#import <fcntl.h>
#import <sys/file.h>

#pragma mark - CGVirtualDisplay 私有接口

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

#pragma mark - 配置

static const double kPhoneW   = 393;   // iPhone 15 Pro 点宽
static const double kPhoneH   = 852;   // iPhone 15 Pro 点高
static const double kSidebar  = 116;   // 窗口右侧工具栏
static const double kTitlebar = 28;
static const double kMenuBar  = 25;    // 虚拟屏顶部留给菜单栏

static NSString *const kScreenName = @"iPhone Virtual";
static NSString *const kOwnerName  = @"iPhone Use";

static CGVirtualDisplay *gVD = nil;
static CGDirectDisplayID gVirtID = 0;
static CGDirectDisplayID gBuiltinID = 0;
static CGRect gBuiltinBounds = {0};
static CGSize gPreferredPt = {1680, 1050};   // 启动时内置屏的点尺寸，作为要维持的目标

static BOOL gVerbose = NO;
#define LOG(fmt, ...) do { if (gVerbose) { NSLog(fmt, ##__VA_ARGS__); } } while (0)

static void onSignal(int sig) {
    if (gVD) { gVD = nil; }
    _exit(0);
}

#pragma mark - 记住内置屏的理想尺寸

static NSString *preferredPath(void) {
    NSString *dir = [[NSBundle mainBundle] bundlePath];
    if (!dir) dir = [[NSProcessInfo processInfo] arguments][0];
    dir = [dir stringByDeletingLastPathComponent];
    return [dir stringByAppendingPathComponent:@".preferred"];
}

static void loadPreferred(void) {
    NSString *s = [NSString stringWithContentsOfFile:preferredPath() encoding:NSUTF8StringEncoding error:NULL];
    if (s) {
        NSString *trimmed = [s stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceAndNewlineCharacterSet]];
        NSArray *parts = [trimmed componentsSeparatedByString:@"x"];
        if (parts.count == 2) {
            gPreferredPt = CGSizeMake([parts[0] doubleValue], [parts[1] doubleValue]);
            return;
        }
    }
    CGDisplayModeRef cur = CGDisplayCopyDisplayMode(gBuiltinID);
    if (cur) {
        gPreferredPt = CGSizeMake(CGDisplayModeGetWidth(cur), CGDisplayModeGetHeight(cur));
        CFRelease(cur);
    }
    NSString *out = [NSString stringWithFormat:@"%.0fx%.0f", gPreferredPt.width, gPreferredPt.height];
    [out writeToFile:preferredPath() atomically:YES encoding:NSUTF8StringEncoding error:NULL];
}

#pragma mark - 找 App

// 不用 NSWorkspace：它靠主 RunLoop 收通知，裸 while 循环里永远拿到启动时的旧快照。
// 直接用 libproc 枚举进程，实时且不依赖事件循环。
static pid_t findAppPID(void) {
    pid_t pids[4096];
    int n = proc_listallpids(pids, (int)sizeof(pids));
    if (n <= 0) return 0;
    pid_t self = getpid();
    for (int i = 0; i < n; i++) {
        pid_t p = pids[i];
        if (p <= 0 || p == self) continue;
        char path[PROC_PIDPATHINFO_MAXSIZE];
        if (proc_pidpath(p, path, sizeof(path)) <= 0) continue;
        if (strstr(path, "iPhoneUse.app") || strstr(path, "iPhone Use.app")) return p;
    }
    return 0;
}

#pragma mark - 虚拟屏

static BOOL createVirtualDisplay(void) {
    CGVirtualDisplayDescriptor *d = [[CGVirtualDisplayDescriptor alloc] init];
    d.name = kScreenName;
    d.maxPixelsWide = (uint32_t)((kPhoneW + kSidebar) * 2);
    d.maxPixelsHigh = (uint32_t)((kPhoneH + kTitlebar + kMenuBar) * 2);
    d.sizeInMillimeters = CGSizeMake(600.0, 600.0 * (kPhoneH + kTitlebar + kMenuBar) / (kPhoneW + kSidebar));
    d.productID = 0x1D15; d.vendorID = 0x4B55; d.serialNum = 1;
    d.queue = dispatch_get_main_queue();

    gVD = [[CGVirtualDisplay alloc] initWithDescriptor:d];
    if (!gVD) { LOG(@"创建虚拟屏失败"); return NO; }

    CGVirtualDisplaySettings *s = [[CGVirtualDisplaySettings alloc] init];
    s.hiDPI = 1;
    s.modes = @[[[CGVirtualDisplayMode alloc] initWithWidth:(uint32_t)(kPhoneW + kSidebar)
                                                    height:(uint32_t)(kPhoneH + kTitlebar + kMenuBar)
                                               refreshRate:60]];
    if (![gVD applySettings:s]) { gVD = nil; LOG(@"应用虚拟屏模式失败"); return NO; }
    gVirtID = gVD.displayID;

    for (int i = 0; i < 40 && !CGDisplayIsActive(gVirtID); i++) usleep(100000);
    usleep(600000);
    return YES;
}

static void destroyVirtualDisplay(void) {
    if (!gVD) return;
    gVD = nil;              // 释放即移除显示器
    gVirtID = 0;
    usleep(400000);
}

/// 内置屏固定 (0,0) 主屏，虚拟屏贴右侧、垂直居中
static BOOL arrangeDisplays(void) {
    if (!gVirtID) return NO;
    gBuiltinBounds = CGDisplayBounds(gBuiltinID);
    double virtH = kPhoneH + kTitlebar + kMenuBar;
    double x = gBuiltinBounds.size.width;
    double y = (gBuiltinBounds.size.height - virtH) / 2.0;

    CGDisplayConfigRef cfg;
    if (CGBeginDisplayConfiguration(&cfg) != kCGErrorSuccess) return NO;
    CGConfigureDisplayOrigin(cfg, gBuiltinID, 0, 0);
    CGConfigureDisplayMirrorOfDisplay(cfg, gVirtID, kCGNullDirectDisplay);
    CGConfigureDisplayOrigin(cfg, gVirtID, (int)x, (int)y);
    return CGCompleteDisplayConfiguration(cfg, kCGConfigureForSession) == kCGErrorSuccess;
}

static NSScreen *virtualScreen(void) {
    for (NSScreen *s in NSScreen.screens)
        if ([s.localizedName isEqualToString:kScreenName]) return s;
    return nil;
}

#pragma mark - 内置屏分辨率守卫

// 加虚拟屏时系统会把内置屏重置成 1440×900，这里负责纠回来

static BOOL builtinModeOK(void) {
    CGDisplayModeRef cur = CGDisplayCopyDisplayMode(gBuiltinID);
    if (!cur) return YES;
    BOOL same = (CGDisplayModeGetWidth(cur) == gPreferredPt.width &&
                 CGDisplayModeGetHeight(cur) == gPreferredPt.height);
    CFRelease(cur);
    return same;
}

static void ensureBuiltinMode(void) {
    if (builtinModeOK()) return;
    NSDictionary *opts = @{ (__bridge NSString *)kCGDisplayShowDuplicateLowResolutionModes: @YES };
    CFArrayRef modes = CGDisplayCopyAllDisplayModes(gBuiltinID, (__bridge CFDictionaryRef)opts);
    if (!modes) return;
    CGDisplayModeRef target = NULL;
    for (id m in (__bridge_transfer NSArray *)modes) {
        CGDisplayModeRef mm = (__bridge CGDisplayModeRef)m;
        // 优先 HiDPI（点尺寸相同、像素是 2 倍）
        if (CGDisplayModeGetWidth(mm) == gPreferredPt.width &&
            CGDisplayModeGetHeight(mm) == gPreferredPt.height &&
            CGDisplayModeGetPixelWidth(mm) == gPreferredPt.width * 2) {
            target = (CGDisplayModeRef)CFRetain((CFTypeRef)mm);
            break;
        }
    }
    if (!target) {   // 退而求其次：任意同点尺寸模式
        CFArrayRef plain = CGDisplayCopyAllDisplayModes(gBuiltinID, NULL);
        for (id m in (__bridge_transfer NSArray *)plain) {
            CGDisplayModeRef mm = (__bridge CGDisplayModeRef)m;
            if (CGDisplayModeGetWidth(mm) == gPreferredPt.width &&
                CGDisplayModeGetHeight(mm) == gPreferredPt.height) {
                target = (CGDisplayModeRef)CFRetain((CFTypeRef)mm);
                break;
            }
        }
    }
    if (!target) { LOG(@"找不到 %.0fx%.0f 模式", gPreferredPt.width, gPreferredPt.height); return; }
    CGDisplaySetDisplayMode(gBuiltinID, target, NULL);
    CFRelease(target);
    usleep(700000);
    LOG(@"内置屏已恢复 %.0fx%.0f", gPreferredPt.width, gPreferredPt.height);
    arrangeDisplays();
}

#pragma mark - 窗口摆放

static AXUIElementRef copyMainWindow(pid_t pid) {
    AXUIElementRef app = AXUIElementCreateApplication(pid);
    CFTypeRef wins = NULL;
    AXUIElementRef win = NULL;
    if (AXUIElementCopyAttributeValue(app, kAXWindowsAttribute, &wins) == kAXErrorSuccess && wins) {
        NSArray *arr = (__bridge_transfer NSArray *)wins;
        if (arr.count > 0) win = (AXUIElementRef)CFBridgingRetain(arr[0]);
    }
    CFRelease(app);
    return win;
}

static BOOL windowFrame(AXUIElementRef win, CGRect *out) {
    CFTypeRef posV = NULL, sizeV = NULL;
    if (AXUIElementCopyAttributeValue(win, kAXPositionAttribute, &posV) != kAXErrorSuccess) return NO;
    if (AXUIElementCopyAttributeValue(win, kAXSizeAttribute, &sizeV) != kAXErrorSuccess) { CFRelease(posV); return NO; }
    CGPoint p = CGPointZero; CGSize s = CGSizeZero;
    AXValueGetValue((AXValueRef)posV, kAXValueCGPointType, &p);
    AXValueGetValue((AXValueRef)sizeV, kAXValueCGSizeType, &s);
    CFRelease(posV); CFRelease(sizeV);
    *out = CGRectMake(p.x, p.y, s.width, s.height);
    return YES;
}

static BOOL setWindowFrame(AXUIElementRef win, CGRect r) {
    CGSize s = r.size;
    AXValueRef sv = AXValueCreate(kAXValueCGSizeType, &s);
    AXError e1 = AXUIElementSetAttributeValue(win, kAXSizeAttribute, sv);
    CFRelease(sv);
    CGPoint p = r.origin;
    AXValueRef pv = AXValueCreate(kAXValueCGPointType, &p);
    AXError e2 = AXUIElementSetAttributeValue(win, kAXPositionAttribute, pv);
    CFRelease(pv);
    return (e1 == kAXErrorSuccess && e2 == kAXErrorSuccess);
}

static CGRect targetFrame(NSScreen *vs) {
    double mainH = NSScreen.screens.firstObject.frame.size.height;
    // 屏幕在 CG 坐标系里的顶边与高度
    double screenTop = mainH - (vs.frame.origin.y + vs.frame.size.height);
    double screenH = vs.frame.size.height;
    // 底部对齐：顶部自然留出 25px 菜单栏，避免被系统钳回去后反复重摆
    double y = screenTop + screenH - (kPhoneH + kTitlebar);
    return CGRectMake(round(vs.frame.origin.x), round(y),
                      kPhoneW + kSidebar, kPhoneH + kTitlebar);
}

/// 最前面的普通窗口属于谁（不需要 RunLoop，比 NSWorkspace 可靠）
static pid_t frontmostPID(void);

/// 主动激活预算：只在启动阶段用来把 App 的 AX 窗口"叫醒"，用完就不再抢焦点
static int gActivateBudget = 4;
/// 为摆放窗口而激活时，被抢走焦点的原应用
static pid_t gActivatedFrom = 0;
/// 是否开启"指针进入手机区就切焦点"（默认关闭，避免误触抢焦点）
static BOOL gFocusFollow = NO;

static BOOL activateApp(pid_t pid, BOOL force) {
    if (!force) {
        if (gActivateBudget <= 0) return NO;
        gActivateBudget--;
    }
    NSRunningApplication *app = [NSRunningApplication runningApplicationWithProcessIdentifier:pid];
    if (!app || app.terminated) return NO;
    pid_t before = frontmostPID();
    if (before > 0 && before != pid && before != getpid()) gActivatedFrom = before;
    [app activateWithOptions:0];
    usleep(400000);
    return YES;
}

/// 把焦点还给摆放前的前台应用
static void restoreFocusAfterPlacement(pid_t appPID) {
    if (gActivatedFrom <= 0 || gActivatedFrom == appPID) return;
    NSRunningApplication *a = [NSRunningApplication runningApplicationWithProcessIdentifier:gActivatedFrom];
    if (a && !a.terminated) {
        [a activateWithOptions:0];
        LOG(@"摆放完成，焦点还给 %d", gActivatedFrom);
    }
    gActivatedFrom = 0;
}

/// 最前面的普通窗口属于谁（不需要 RunLoop，比 NSWorkspace 可靠）
static pid_t frontmostPID(void) {
    CFArrayRef list = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements, kCGNullWindowID);
    if (!list) return 0;
    pid_t pid = 0;
    for (NSDictionary *d in (__bridge NSArray *)list) {
        if ([d[(id)kCGWindowLayer] integerValue] != 0) continue;
        NSString *owner = d[(id)kCGWindowOwnerName] ?: @"";
        // 跳过桌面/壁纸/Dock 等系统窗口
        if ([owner isEqualToString:@"Window Server"] || [owner isEqualToString:@"Dock"] ||
            [owner isEqualToString:@"Wallpaper"] || [owner isEqualToString:@"SystemUIServer"] ||
            [owner isEqualToString:@"Control Center"] || [owner isEqualToString:@"Notification Center"])
            continue;
        NSDictionary *b = d[(id)kCGWindowBounds];
        if ([b[@"Width"] doubleValue] < 2 || [b[@"Height"] doubleValue] < 2) continue;
        pid = [d[(id)kCGWindowOwnerPID] intValue];
        break;
    }
    CFRelease(list);
    return pid;
}

/// 返回 YES 表示已就位
static BOOL placeWindow(pid_t pid, BOOL allowActivate) {
    NSScreen *vs = virtualScreen();
    if (!vs) return NO;

    AXUIElementRef win = copyMainWindow(pid);
    if (!win) {
        if (!allowActivate) return NO;
        if (!activateApp(pid, NO)) return NO;   // 预算用完就不再打扰用户
        win = copyMainWindow(pid);
        if (!win) { restoreFocusAfterPlacement(pid); return NO; }
    }

    CGRect want = targetFrame(vs);
    CGRect got = CGRectZero;
    BOOL ok = NO;

    if (windowFrame(win, &got) &&
        fabs(got.origin.x - want.origin.x) < 1 &&
        fabs(got.origin.y - want.origin.y) < 1 &&
        fabs(got.size.width - want.size.width) < 1 &&
        fabs(got.size.height - want.size.height) < 1) {
        ok = YES;
    } else {
        // 先退出全屏，再加尺寸、定位
        CFTypeRef f = NULL;
        if (AXUIElementCopyAttributeValue(win, CFSTR("AXFullScreen"), &f) == kAXErrorSuccess && f) {
            if ([(__bridge NSNumber *)f boolValue])
                AXUIElementSetAttributeValue(win, CFSTR("AXFullScreen"), kCFBooleanFalse);
            CFRelease(f);
            usleep(600000);
        }
        AXUIElementSetAttributeValue(win, kAXMinimizedAttribute, kCFBooleanFalse);
        setWindowFrame(win, want);
        usleep(300000);
        if (windowFrame(win, &got))
            ok = fabs(got.size.width - want.size.width) < 2 && fabs(got.size.height - want.size.height) < 2;
        if (!ok) { setWindowFrame(win, want); usleep(250000); }
        LOG(@"place want=%.0f,%.0f %.0fx%.0f got=%.0f,%.0f %.0fx%.0f",
            want.origin.x, want.origin.y, want.size.width, want.size.height,
            got.origin.x, got.origin.y, got.size.width, got.size.height);
    }
    CFRelease(win);
    return ok;
}

#pragma mark - 光标接管

static CGRect phoneRectOnScreen(void) {
    NSScreen *vs = virtualScreen();
    if (!vs) return CGRectZero;
    CGRect w = targetFrame(vs);
    return CGRectMake(w.origin.x, w.origin.y + kTitlebar, kPhoneW, kPhoneH);
}

static BOOL pointInAppWindow(CGPoint p, pid_t pid) {
    CFArrayRef list = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements, kCGNullWindowID);
    if (!list) return NO;
    BOOL hit = NO;
    // 注意：这里必须用 __bridge，所有权仍归 CF；用 __bridge_transfer 再加 CFRelease 会双重释放
    for (NSDictionary *d in (__bridge NSArray *)list) {
        if ([d[(id)kCGWindowLayer] integerValue] != 0) continue;
        NSString *owner = d[(id)kCGWindowOwnerName] ?: @"";
        if ([owner isEqualToString:@"Window Server"] || [owner isEqualToString:@"Dock"] ||
            [owner isEqualToString:@"Wallpaper"]) continue;
        NSDictionary *b = d[(id)kCGWindowBounds];
        CGRect r = CGRectMake([b[@"X"] doubleValue], [b[@"Y"] doubleValue],
                              [b[@"Width"] doubleValue], [b[@"Height"] doubleValue]);
        if (CGRectContainsPoint(r, p)) {
            hit = ([d[(id)kCGWindowOwnerPID] intValue] == pid);
            break;
        }
    }
    CFRelease(list);
    return hit;
}

#pragma mark - 主循环

int main(int argc, const char **argv) {
    @autoreleasepool {
        for (int i = 1; i < argc; i++) {
            if (strcmp(argv[i], "-v") == 0) gVerbose = YES;
            else if (strcmp(argv[i], "--follow") == 0) gFocusFollow = YES;
        }
        signal(SIGTERM, onSignal);
        signal(SIGINT, onSignal);

        // 单实例：开机自启与手动启动同时存在时，只有先来的能建屏
        NSString *lockPath = [[preferredPath() stringByDeletingLastPathComponent] stringByAppendingPathComponent:@".psagent.lock"];
        int lockFD = open(lockPath.fileSystemRepresentation, O_CREAT | O_RDWR, 0644);
        if (lockFD < 0 || flock(lockFD, LOCK_EX | LOCK_NB) != 0) {
            fprintf(stderr, "已有实例在运行，退出\n");
            return 0;
        }

        gBuiltinID = CGMainDisplayID();
        gBuiltinBounds = CGDisplayBounds(gBuiltinID);
        loadPreferred();
        LOG(@"启动: builtin=%u %.0fx%.0f 目标=%.0fx%.0f", gBuiltinID, gBuiltinBounds.size.width, gBuiltinBounds.size.height,
            gPreferredPt.width, gPreferredPt.height);

        pid_t lastPID = 0;
        double lastPlaceTry = 0, lastModeCheck = 0, appSeenAt = 0;
        BOOL placed = NO;
        BOOL cursorHidden = NO;
        BOOL wasInside = NO;
        pid_t prevFrontPID = 0;

        while (1) {
            double now = CFAbsoluteTimeGetCurrent();
            pid_t pid = findAppPID();

            if (pid != lastPID) {
                LOG(@"app pid: %d -> %d", lastPID, pid);
                lastPID = pid;
                placed = NO;
                lastPlaceTry = 0;
                wasInside = NO;
                prevFrontPID = 0;
                gActivateBudget = 4;   // 新一次启动，重新给几次"叫醒"预算
                if (!pid) {
                    if (cursorHidden) { CGDisplayShowCursor(CGMainDisplayID()); cursorHidden = NO; }
                    destroyVirtualDisplay();
                    continue;
                }
                appSeenAt = now;
            }

            if (!pid) { usleep(500000); continue; }

            // 1) 确保虚拟屏
            if (!gVirtID) {
                if (!createVirtualDisplay()) { usleep(2000000); continue; }
                arrangeDisplays();
                usleep(300000);
                ensureBuiltinMode();     // 加屏会把内置屏压低，这里纠回来
                usleep(300000);
                arrangeDisplays();
                placed = NO;
            }

            // 1b) 每秒守一次内置屏分辨率
            if (now - lastModeCheck > 1.0) {
                lastModeCheck = now;
                ensureBuiltinMode();
            }

            // 2) 摆窗口：刚启动时 0.8s 一次（有激活预算兜着），25s 后仍失败就退到 5s 一次
            double interval;
            if (placed) interval = 4.0;
            else interval = (now - appSeenAt < 25.0) ? 0.8 : 5.0;

            if (now - lastPlaceTry > interval) {
                lastPlaceTry = now;
                BOOL ok = placeWindow(pid, !placed);
                if (ok && !placed) {
                    placed = YES;
                    LOG(@"窗口已就位");
                    restoreFocusAfterPlacement(pid);   // 摆放用的临时激活，立刻还回去
                }
                if (!ok && placed) { placed = NO; }
            }

            // 3) 光标 + 焦点跟随
            CGRect pr = phoneRectOnScreen();
            BOOL inside = NO;
            if (!CGRectEqualToRect(pr, CGRectZero)) {
                CGEventRef ev = CGEventCreate(NULL);
                CGPoint p = CGEventGetLocation(ev);
                CFRelease(ev);
                inside = CGRectContainsPoint(pr, p) && pointInAppWindow(p, pid);
            }

            // 进入手机画面：默认不动焦点；只有加 --follow 才把键盘交给 iPhone Use
            if (gFocusFollow) {
                if (inside && !wasInside) {
                    pid_t fp = frontmostPID();
                    if (fp != pid) {
                        prevFrontPID = fp;
                        activateApp(pid, YES);
                        LOG(@"进入手机区，焦点交给 App（原 %d）", fp);
                    }
                } else if (!inside && wasInside) {
                    if (prevFrontPID > 0) {
                        NSRunningApplication *a = [NSRunningApplication runningApplicationWithProcessIdentifier:prevFrontPID];
                        if (a && !a.terminated) {
                            [a activateWithOptions:0];
                            LOG(@"离开手机区，焦点还给 %d", prevFrontPID);
                        }
                    }
                    prevFrontPID = 0;
                }
            }
            wasInside = inside;

            // 光标：只在手机画面区且窗口在最前面时隐藏本机指针
            if (inside && !cursorHidden) { CGDisplayHideCursor(CGMainDisplayID()); cursorHidden = YES; }
            else if (!inside && cursorHidden) { CGDisplayShowCursor(CGMainDisplayID()); cursorHidden = NO; }

            usleep(16000);
        }
    }
    return 0;
}
