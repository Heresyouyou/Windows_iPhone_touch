# macOS 侧构建
# 用法: make            全部
#       make psagent    只构建守护进程
#       make memtest && ./memtest   跑回归测试

CC      := clang
CFLAGS  := -fobjc-arc -O2 -Wall
FRAMEWORKS_COCOA   := -framework Cocoa -framework CoreGraphics
FRAMEWORKS_AX      := $(FRAMEWORKS_COCOA) -framework ApplicationServices

ALL := psagent vdisplay phonescreen appctl disps axdump memtest

.PHONY: all clean test

all: $(ALL)

psagent: psagent.m
	$(CC) $(CFLAGS) $(FRAMEWORKS_AX) -o $@ $<

phonescreen: phonescreen.m
	$(CC) $(CFLAGS) $(FRAMEWORKS_AX) -o $@ $<

vdisplay: vdisplay.m
	$(CC) $(CFLAGS) $(FRAMEWORKS_COCOA) -o $@ $<

appctl: appctl.m
	$(CC) $(CFLAGS) $(FRAMEWORKS_COCOA) -o $@ $<

disps: disps.m
	$(CC) $(CFLAGS) $(FRAMEWORKS_COCOA) -o $@ $<

axdump: axdump.m
	$(CC) $(CFLAGS) $(FRAMEWORKS_AX) -o $@ $<

# CoreFoundation 所有权回归测试：非零退出即说明又出现了双重释放
memtest: memtest.m
	$(CC) $(CFLAGS) $(FRAMEWORKS_COCOA) -o $@ $<

test: memtest
	./memtest

clean:
	rm -f $(ALL)
