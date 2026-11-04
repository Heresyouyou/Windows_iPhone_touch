// axdump — 诊断：打印某个进程的 AX 窗口列表与相关属性
#import <Cocoa/Cocoa.h>

int main(int argc, const char **argv) {
    @autoreleasepool {
        pid_t pid = argc > 1 ? atoi(argv[1]) : 0;
        if (!pid) { printf("usage: axdump <pid>\n"); return 2; }
        AXUIElementRef app = AXUIElementCreateApplication(pid);
        printf("AX trusted: %d\n", AXIsProcessTrusted());

        CFTypeRef wins = NULL;
        AXError e = AXUIElementCopyAttributeValue(app, kAXWindowsAttribute, &wins);
        printf("AXWindows err=%d type=%s count=", (int)e, wins ? CFGetTypeID(wins) == CFArrayGetTypeID() ? "array" : "other" : "null");
        if (wins && CFGetTypeID(wins) == CFArrayGetTypeID()) printf("%ld", (long)CFArrayGetCount((CFArrayRef)wins));
        printf("\n");

        if (wins && CFGetTypeID(wins) == CFArrayGetTypeID()) {
            CFArrayRef arr = (CFArrayRef)wins;
            for (CFIndex i = 0; i < CFArrayGetCount(arr); i++) {
                AXUIElementRef w = (AXUIElementRef)CFArrayGetValueAtIndex(arr, i);
                CFTypeRef t = NULL, r = NULL, sub = NULL, min = NULL;
                AXUIElementCopyAttributeValue(w, kAXTitleAttribute, &t);
                AXUIElementCopyAttributeValue(w, kAXRoleAttribute, &r);
                AXUIElementCopyAttributeValue(w, kAXSubroleAttribute, &sub);
                AXUIElementCopyAttributeValue(w, kAXMinimizedAttribute, &min);
                CFTypeRef pv = NULL, sv = NULL;
                AXUIElementCopyAttributeValue(w, kAXPositionAttribute, &pv);
                AXUIElementCopyAttributeValue(w, kAXSizeAttribute, &sv);
                CGPoint p = CGPointZero; CGSize s = CGSizeZero;
                if (pv) AXValueGetValue((AXValueRef)pv, kAXValueCGPointType, &p);
                if (sv) AXValueGetValue((AXValueRef)sv, kAXValueCGSizeType, &s);
                printf("  [%ld] role=%s sub=%s title=%s min=%d pos=%.0f,%.0f size=%.0fx%.0f\n", (long)i,
                       r ? [(__bridge NSString *)r UTF8String] : "?",
                       sub ? [(__bridge NSString *)sub UTF8String] : "?",
                       t ? [(__bridge NSString *)t UTF8String] : "",
                       min ? [(__bridge NSNumber *)min boolValue] : -1,
                       p.x, p.y, s.width, s.height);
                if (t) CFRelease(t); if (r) CFRelease(r); if (sub) CFRelease(sub); if (min) CFRelease(min);
                if (pv) CFRelease(pv); if (sv) CFRelease(sv);
            }
        }
        if (wins) CFRelease(wins);
        CFRelease(app);
    }
    return 0;
}
