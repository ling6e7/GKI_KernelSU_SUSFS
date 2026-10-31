# SPDX-License-Identifier: GPL-2.0-only
#
# Standalone PMIC-glink battery-charger property probe.
#
# Deliberately a separate module (and a separate platform_driver name) so it
# can be insmod'ed next to the stock qti_battery_charger_main_m81 and rmmod'ed
# cleanly afterwards: nothing depends on it.

ifneq ($(KERNELRELEASE),)

obj-m := xm_probe.o

ccflags-y += -I$(src)/include
ccflags-y += -Wno-unused-function -Wno-unused-variable
ccflags-y += -DCONFIG_QTI_PMIC_GLINK=1

else

KDIR ?= $(abspath $(CURDIR)/../../out/android12-5.10/common)
PWD  := $(shell pwd)
ARCH ?= arm64
LLVM_IAS ?= 1
CROSS_COMPILE ?= aarch64-linux-gnu-

all:
	@echo "== KDIR = $(KDIR) =="
	@test -f "$(KDIR)/.config" || { echo "ERROR: $(KDIR)/.config missing"; exit 1; }
	$(MAKE) -C $(KDIR) M=$(PWD) ARCH=$(ARCH) LLVM=1 LLVM_IAS=$(LLVM_IAS) \
		CROSS_COMPILE=$(CROSS_COMPILE) \
		KBUILD_MODPOST_WARN=1 modules

clean:
	$(MAKE) -C $(KDIR) M=$(PWD) ARCH=$(ARCH) clean

.PHONY: all clean

endif
