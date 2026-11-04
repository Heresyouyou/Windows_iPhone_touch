#import <Cocoa/Cocoa.h>
int main(){ @autoreleasepool {
  uint32_t n=0; CGGetActiveDisplayList(0,NULL,&n);
  CGDirectDisplayID ids[8]={0}; CGGetActiveDisplayList(8,ids,&n);
  printf("active=%u main=%u\n", n, CGMainDisplayID());
  for(uint32_t i=0;i<n;i++){
    CGRect b=CGDisplayBounds(ids[i]);
    CGDisplayModeRef m=CGDisplayCopyDisplayMode(ids[i]);
    printf("id=%u bounds=%.0f,%.0f %.0fx%.0f px=%zux%zu mode=%zux%zu main=%d\n", ids[i],
      b.origin.x,b.origin.y,b.size.width,b.size.height,
      CGDisplayPixelsWide(ids[i]),CGDisplayPixelsHigh(ids[i]),
      m?CGDisplayModeGetWidth(m):0, m?CGDisplayModeGetHeight(m):0, CGDisplayIsMain(ids[i]));
    if(m) CFRelease(m);
  }
  for (NSScreen *s in NSScreen.screens) printf("  NSScreen %s frame=%s\n", s.localizedName.UTF8String, [[NSString stringWithFormat:@"%.0f,%.0f %.0fx%.0f", s.frame.origin.x,s.frame.origin.y,s.frame.size.width,s.frame.size.height] UTF8String]);
} return 0; }
