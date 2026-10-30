# SPDX-License-Identifier: GPL-2.0-only
#
# Out-of-tree build of Xiaomi's QTI battery charger driver against the
# AOSP GKI kernel tree, producing qti_battery_charger_main_m81.ko.
#
# The module name matches what the device expects in
# /vendor_dlkm/lib/modules/modules.load.
#
# pmic_glink / panel_event_notifier symbols are resolved at load time from
# the vendor-supplied modules already present on the device, so modpost is
# expected to warn about them -> build with KBUILD_MODPOST_WARN=1.

ifneq ($(KERNELRELEASE),)

obj-m := qti_battery_charger_main_m81.o
qti_battery_charger_main_m81-y := qti_battery_charger.o

ccflags-y += -I$(src)/include
ccflags-y += -Wno-unused-function -Wno-unused-variable
ccflags-y += -DCONFIG_QTI_PMIC_GLINK=1
ccflags-y += -DCONFIG_QTI_BATTERY_CHARGER=1
ccflags-y += -DCONFIG_QCOM_PANEL_EVENT_NOTIFIER=1

else

# KDIR must point at the kernel *build* output directory produced by
# build/build.sh (it holds .config, Module.symvers and the generated headers).
KDIR ?= $(abspath $(CURDIR)/../../out/android12-5.10)
PWD  := $(shell pwd)
ARCH ?= arm64

all:
	@echo "== KDIR = $(KDIR) =="
	@test -f "$(KDIR)/.config" || { echo "ERROR: $(KDIR)/.config missing"; exit 1; }
	$(MAKE) -C $(KDIR) M=$(PWD) ARCH=$(ARCH) LLVM=1 \
		KBUILD_MODPOST_WARN=1 modules

modules_install:
	$(MAKE) -C $(KDIR) M=$(PWD) ARCH=$(ARCH) LLVM=1 \
		KBUILD_MODPOST_WARN=1 modules_install

clean:
	$(MAKE) -C $(KDIR) M=$(PWD) ARCH=$(ARCH) clean

.PHONY: all modules_install clean

endif
