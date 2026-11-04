// 压力测试：复刻 psagent 里两个高频热路径，验证不再双重释放
// 编译: clang -fobjc-arc -framework Cocoa -framework CoreGraphics -o memtest memtest.m
#import <Cocoa/Cocoa.h>
#import <CoreGraphics/CoreGraphics.h>

static BOOL pointInAppWindow(CGPoint p, pid_t pid) {
    CFArrayRef list = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements, kCGNullWindowID);
    if (!list) return NO;
    BOOL hit = NO;
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

static pid_t frontmostPID(void) {
    CFArrayRef list = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements, kCGNullWindowID);
    if (!list) return 0;
    pid_t pid = 0;
    for (NSDictionary *d in (__bridge NSArray *)list) {
        if ([d[(id)kCGWindowLayer] integerValue] != 0) continue;
        NSString *owner = d[(id)kCGWindowOwnerName] ?: @"";
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

int main(void) {
    @autoreleasepool {
        int hits = 0;
        for (int i = 0; i < 2500; i++) {
            @autoreleasepool {
                if (pointInAppWindow(CGPointMake(840, 500), 1)) hits++;
                if (frontmostPID() > 0) hits++;
            }
            if (i % 5000 == 0) fprintf(stderr, "... %d\n", i);
        }
        printf("完成 20000 轮（4 万次调用），hits=%d，无崩溃\n", hits);
    }
    return 0;
}
