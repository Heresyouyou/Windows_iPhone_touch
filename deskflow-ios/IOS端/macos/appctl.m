// appctl — 用 NSRunningApplication 优雅地退出/启动 iPhone Use（绕开 Apple Events 权限）
// 用法: appctl quit | appctl open
#import <Cocoa/Cocoa.h>

static NSRunningApplication *findApp(void) {
    for (NSRunningApplication *a in [[NSWorkspace sharedWorkspace] runningApplications]) {
        NSString *bid = (a.bundleIdentifier ?: @"").lowercaseString;
        NSString *nm = (a.localizedName ?: @"").lowercaseString;
        if ([bid containsString:@"iphone-use"] || [nm containsString:@"iphone use"]) {
            if (!a.terminated) return a;
        }
    }
    return nil;
}

int main(int argc, const char **argv) {
    @autoreleasepool {
        NSString *cmd = argc > 1 ? [NSString stringWithUTF8String:argv[1]] : @"status";
        if ([cmd isEqualToString:@"quit"]) {
            NSRunningApplication *a = findApp();
            if (!a) { printf("not running\n"); return 0; }
            BOOL ok = [a terminate];
            printf("terminate -> %d (pid %d)\n", ok, a.processIdentifier);
        } else if ([cmd isEqualToString:@"open"]) {
            NSURL *u = [NSURL fileURLWithPath:@"/Applications/iPhoneUse.app"];
            NSWorkspaceOpenConfiguration *c = [NSWorkspaceOpenConfiguration configuration];
            [[NSWorkspace sharedWorkspace] openApplicationAtURL:u configuration:c
                                              completionHandler:^(NSRunningApplication *app, NSError *err) {
                printf("open -> %s\n", err ? err.localizedDescription.UTF8String : "ok");
            }];
            [[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:2.0]];
        } else {
            NSRunningApplication *a = findApp();
            printf("%s pid=%d\n", a ? "running" : "not running", a ? a.processIdentifier : 0);
        }
    }
    return 0;
}
