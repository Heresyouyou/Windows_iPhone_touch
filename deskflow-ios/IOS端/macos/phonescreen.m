// phonescreen — 把 iPhone Use 窗口摆成"贴边的副屏"，并在鼠标进入时隐藏本机光标
//
// 用法:
//   phonescreen info
//   phonescreen place  [--pid N] [--phone-w 393] [--phone-h 852] [--sidebar 116] [--titlebar 28] [--edge right|left]
//   phonescreen bridge [--pid N] [--phone-w 393] [--phone-h 852] [--sidebar 116] [--titlebar 28] [--edge right|left]
//   phonescreen run    等同于 place 后常驻 bridge

#import <Cocoa/Cocoa.h>
#import <ApplicationServices/ApplicationServices.h>

static NSString *ownerName = @"iPhoneUse";

typedef struct {
    double phoneW, phoneH, sidebar, titlebar;
    BOOL rightEdge;
} Config;

static NSString *targetScreenName = nil;

static Config cfg = {393, 852, 116, 28, YES};
static pid_t targetPID = 0;
static BOOL quiet = NO;

static BOOL parseArgs(int argc, const char **argv) {
    for (int i = 1; i < argc; i++) {
        NSString *a = [NSString stringWithUTF8String:argv[i]];
        if ([a isEqualToString:@"--pid"] && i + 1 < argc) targetPID = atoi(argv[++i]);
        else if ([a isEqualToString:@"--phone-w"] && i + 1 < argc) cfg.phoneW = atof(argv[++i]);
        else if ([a isEqualToString:@"--phone-h"] && i + 1 < argc) cfg.phoneH = atof(argv[++i]);
        else if ([a isEqualToString:@"--sidebar"] && i + 1 < argc) cfg.sidebar = atof(argv[++i]);
        else if ([a isEqualToString:@"--titlebar"] && i + 1 < argc) cfg.titlebar = atof(argv[++i]);
        else if ([a isEqualToString:@"--edge"] && i + 1 < argc) cfg.rightEdge = (strcmp(argv[++i], "left") != 0);
        else if ([a isEqualToString:@"--screen"] && i + 1 < argc) targetScreenName = [NSString stringWithUTF8String:argv[++i]];
    }
    return YES;
}

static NSString *normalized(NSString *s) {
    NSMutableString *out = [NSMutableString string];
    for (NSUInteger i = 0; i < s.length; i++) {
        unichar c = [s characterAtIndex:i];
        if ([[NSCharacterSet alphanumericCharacterSet] characterIsMember:c]) {
            [out appendFormat:@"%C", c];
        }
    }
    return out.lowercaseString;
}

static pid_t findTargetPID(void) {
    if (targetPID > 0) return targetPID;
    NSArray *apps = [[NSWorkspace sharedWorkspace] runningApplications];
    for (NSRunningApplication *app in apps) {
        NSString *name = normalized(app.localizedName ?: @"");
        NSString *bid = normalized(app.bundleIdentifier ?: @"");
        if ([name containsString:@"iphoneuse"] || [bid containsString:@"iphoneuse"]) {
            return app.processIdentifier;
        }
    }
    return 0;
}

static AXUIElementRef copyMainWindow(pid_t pid) {
    AXUIElementRef app = AXUIElementCreateApplication(pid);
    CFTypeRef wins = NULL;
    AXError err = AXUIElementCopyAttributeValue(app, kAXWindowsAttribute, &wins);
    if (err != kAXErrorSuccess || !wins) {
        fprintf(stderr, "AXWindows error=%d (pid %d)\n", (int)err, pid);
        CFTypeRef focused = NULL;
        AXError e2 = AXUIElementCopyAttributeValue(app, kAXFocusedWindowAttribute, &focused);
        fprintf(stderr, "AXFocusedWindow error=%d\n", (int)e2);
        if (e2 == kAXErrorSuccess && focused) {
            CFRelease(app);
            return (AXUIElementRef)focused;
        }
        CFRelease(app);
        return NULL;
    }
    NSArray *arr = (__bridge_transfer NSArray *)wins;
    AXUIElementRef win = NULL;
    if (arr.count > 0) win = (AXUIElementRef)CFBridgingRetain(arr[0]);
    CFRelease(app);
    return win;
}

static BOOL windowFrame(AXUIElementRef win, CGRect *out) {
    CFTypeRef posV = NULL, sizeV = NULL;
    if (AXUIElementCopyAttributeValue(win, kAXPositionAttribute, &posV) != kAXErrorSuccess) return NO;
    if (AXUIElementCopyAttributeValue(win, kAXSizeAttribute, &sizeV) != kAXErrorSuccess) {
        CFRelease(posV); return NO;
    }
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

    if (e1 != kAXErrorSuccess || e2 != kAXErrorSuccess) {
        fprintf(stderr, "AX set failed: size=%d pos=%d\n", (int)e1, (int)e2);
        return NO;
    }
    return YES;
}

static CGRect screenFrame(void) {
    return CGDisplayBounds(CGMainDisplayID()); // 全局坐标，左上原点
}

// 手机画面在全局坐标中的矩形
static CGRect phoneRect(CGRect win) {
    double x = cfg.rightEdge ? (win.origin.x + win.size.width - cfg.sidebar - cfg.phoneW) : win.origin.x;
    return CGRectMake(x, win.origin.y + cfg.titlebar, cfg.phoneW, cfg.phoneH);
}

static BOOL pointInAnyWindowOfPID(CGPoint p, pid_t pid) {
    CFArrayRef list = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements, kCGNullWindowID);
    if (!list) return NO;
    BOOL hit = NO;
    for (NSDictionary *d in (__bridge_transfer NSArray *)list) {
        NSNumber *layer = d[(id)kCGWindowLayer];
        if (layer.integerValue != 0) continue;
        NSNumber *owner = d[(id)kCGWindowOwnerPID];
        NSDictionary *b = d[(id)kCGWindowBounds];
        CGRect r = CGRectMake([b[@"X"] doubleValue], [b[@"Y"] doubleValue],
                              [b[@"Width"] doubleValue], [b[@"Height"] doubleValue]);
        if (CGRectContainsPoint(r, p)) {
            hit = (owner.intValue == pid);
            break; // 只看最前面的普通窗口
        }
    }
    return hit;
}

static void cmdInfo(void) {
    CGRect s = screenFrame();
    printf("screen: %.0f x %.0f (points)\n", s.size.width, s.size.height);
    pid_t pid = findTargetPID();
    printf("target pid: %d\n", pid);
    AXUIElementRef win = pid ? copyMainWindow(pid) : NULL;
    if (win) {
        CGRect r;
        if (windowFrame(win, &r)) printf("window: x=%.0f y=%.0f w=%.0f h=%.0f\n", r.origin.x, r.origin.y, r.size.width, r.size.height);
        CFTypeRef t = NULL, f = NULL, m = NULL;
        if (AXUIElementCopyAttributeValue(win, kAXTitleAttribute, &t) == kAXErrorSuccess && t) {
            printf("title: %s\n", [(__bridge NSString *)t UTF8String]);
            CFRelease(t);
        }
        if (AXUIElementCopyAttributeValue(win, CFSTR("AXFullScreen"), &f) == kAXErrorSuccess && f) {
            printf("fullscreen: %d\n", [(__bridge NSNumber *)f boolValue]);
            CFRelease(f);
        }
        if (AXUIElementCopyAttributeValue(win, kAXMinimizedAttribute, &m) == kAXErrorSuccess && m) {
            printf("minimized: %d\n", [(__bridge NSNumber *)m boolValue]);
            CFRelease(m);
        }
        CFRelease(win);
    } else {
        printf("window: not available (need Accessibility permission)\n");
    }
    printf("AX trusted: %s\n", AXIsProcessTrusted() ? "yes" : "no");
}

static BOOL cmdPlace(void) {
    if (!AXIsProcessTrusted()) {
        NSDictionary *opts = @{(__bridge id)kAXTrustedCheckOptionPrompt: @YES};
        AXIsProcessTrustedWithOptions((__bridge CFDictionaryRef)opts);
        fprintf(stderr, "需要辅助功能权限：请在系统设置 → 隐私与安全性 → 辅助功能 中勾选运行本程序的 App（Terminal/WorkBuddy），然后重试。\n");
        return NO;
    }
    pid_t pid = findTargetPID();
    if (!pid) { if (!quiet) fprintf(stderr, "未找到 iPhone Use 进程\n"); return NO; }
    AXUIElementRef win = copyMainWindow(pid);
    if (!win) { if (!quiet) fprintf(stderr, "取不到窗口（可能缺辅助功能权限）\n"); return NO; }

    CGRect s = screenFrame();
    double totalW = cfg.phoneW + cfg.sidebar;
    double totalH = cfg.phoneH + cfg.titlebar;
    double x, y;
    if (targetScreenName) {
        NSScreen *found = nil;
        for (NSScreen *sc in NSScreen.screens) {
            if ([sc.localizedName isEqualToString:targetScreenName] ||
                [sc.localizedName containsString:targetScreenName]) { found = sc; break; }
        }
        if (!found) {
            if (!quiet) fprintf(stderr, "找不到名为 %s 的显示器（虚拟屏可能还没建好）\n", targetScreenName.UTF8String);
            CFRelease(win);
            return NO;
        }
        NSScreen *mainScreen = NSScreen.screens.firstObject;
        double mainH = mainScreen.frame.size.height;
        double cgX = found.frame.origin.x;
        double cgY = mainH - (found.frame.origin.y + found.frame.size.height);
        // 标题栏窗口会被约束在可视区域内（顶部会留出菜单栏高度），因此对齐可视区顶部
        double visTop = mainH - (found.visibleFrame.origin.y + found.visibleFrame.size.height);
        x = cgX;
        y = visTop;
        printf("visible frame: y=%.0f h=%.0f (top=%.0f)\n", found.visibleFrame.origin.y, found.visibleFrame.size.height, visTop);
        printf("screen %s: frame=%.0f,%.0f %.0fx%.0f -> cg=%.0f,%.0f\n",
               found.localizedName.UTF8String, found.frame.origin.x, found.frame.origin.y,
               found.frame.size.width, found.frame.size.height, cgX, cgY);
    } else {
        x = cfg.rightEdge ? (s.size.width - cfg.phoneW) : 0;
        y = (s.size.height - cfg.phoneH) / 2.0 - cfg.titlebar;
    }
    CGRect target = CGRectMake(round(x), round(y), round(totalW), round(totalH));

    // 先退出全屏 / 取消最小化，否则设不了尺寸
    CFTypeRef f = NULL;
    if (AXUIElementCopyAttributeValue(win, CFSTR("AXFullScreen"), &f) == kAXErrorSuccess && f) {
        if ([(__bridge NSNumber *)f boolValue]) {
            AXUIElementSetAttributeValue(win, CFSTR("AXFullScreen"), kCFBooleanFalse);
            usleep(900000);
        }
        CFRelease(f);
    }
    AXUIElementSetAttributeValue(win, kAXMinimizedAttribute, kCFBooleanFalse);
    usleep(300000);

    if (!setWindowFrame(win, target)) { CFRelease(win); return NO; }
    usleep(400000);
    CGRect got;
    if (windowFrame(win, &got) && (fabs(got.size.width - totalW) > 2 || fabs(got.size.height - totalH) > 2)) {
        // AppKit 可能把窗口拉回屏幕内，再试一次
        setWindowFrame(win, target);
        usleep(400000);
        windowFrame(win, &got);
    }
    if (windowFrame(win, &got)) {
        printf("placed: x=%.0f y=%.0f w=%.0f h=%.0f\n", got.origin.x, got.origin.y, got.size.width, got.size.height);
        CGRect pr = phoneRect(got);
        printf("phone area: x=%.0f y=%.0f w=%.0f h=%.0f\n", pr.origin.x, pr.origin.y, pr.size.width, pr.size.height);
    }
    CFRelease(win);
    return YES;
}

static BOOL cmdBridge(void) {
    pid_t pid = findTargetPID();
    if (!pid) { if (!quiet) fprintf(stderr, "未找到 iPhone Use 进程\n"); return NO; }
    BOOL hidden = NO;
    CGRect cached = CGRectZero;
    CFAbsoluteTime lastCheck = 0;
    CFAbsoluteTime missingSince = 0;

    while (1) {
        CFAbsoluteTime now = CFAbsoluteTimeGetCurrent();
        if (now - lastCheck > 1.0) {
            lastCheck = now;
            AXUIElementRef win = copyMainWindow(pid);
            CGRect r = CGRectZero;
            BOOL ok = NO;
            if (win) {
                ok = windowFrame(win, &r);
                CFRelease(win);
            }
            if (ok) { cached = r; missingSince = 0; }
            else if (missingSince == 0) { missingSince = now; }
            else if (now - missingSince > 2.0) {
                if (hidden) { CGDisplayShowCursor(CGMainDisplayID()); hidden = NO; }
                return NO; // 窗口没了，交给上层重试
            }
        }
        if (CGRectEqualToRect(cached, CGRectZero)) { usleep(100000); continue; }

        CGRect pr = phoneRect(cached);
        CGEventRef ev = CGEventCreate(NULL);
        CGPoint p = CGEventGetLocation(ev);
        CFRelease(ev);

        // 只有窗口确实在该点最前面、且属于目标进程时才算进入
        BOOL inside = CGRectContainsPoint(pr, p) && pointInAnyWindowOfPID(p, pid);
        if (inside && !hidden) { CGDisplayHideCursor(CGMainDisplayID()); hidden = YES; }
        else if (!inside && hidden) { CGDisplayShowCursor(CGMainDisplayID()); hidden = NO; }

        usleep(16000); // ~60Hz
    }
}

int main(int argc, const char **argv) {
    @autoreleasepool {
        parseArgs(argc, argv);
        NSString *cmd = argc > 1 ? [NSString stringWithUTF8String:argv[1]] : @"info";

        if ([cmd isEqualToString:@"info"]) {
            cmdInfo();
        } else if ([cmd isEqualToString:@"place"]) {
            return cmdPlace() ? 0 : 1;
        } else if ([cmd isEqualToString:@"bridge"]) {
            cmdBridge();
        } else if ([cmd isEqualToString:@"run"]) {
            quiet = YES;
            while (1) {
                if (cmdPlace()) {
                    cmdBridge();
                }
                sleep(2); // 等 App 启动或窗口重建
            }
        } else {
            fprintf(stderr, "usage: phonescreen info|place|bridge|run [options]\n");
            return 2;
        }
    }
    return 0;
}
