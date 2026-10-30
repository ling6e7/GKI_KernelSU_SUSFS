#!/usr/bin/env python3
"""
旁路供电（Battery Idle / Hardware Bypass）内核补丁注入器

目标文件: drivers/power/supply/power_supply_sysfs.c (GKI 内核核心, 编入 boot.img 的 Image)

=============================================================================
实测结论（Xiaomi 12S / mayfly / SM8475 / HyperOS OS3.0.3.0 / 5.10.236）
=============================================================================
在 9V HVDCP 充电器上做的单节点隔离测试（暂停 ACC + Scene 守护进程后逐项验证）：

  C0  全关基线                      USB 8.62V @ 1.60A   电池 -2.60A   Charging
  C1  night_charging=1              USB 8.97V @ 0.15A   电池     0mA   <- 真旁路
  C2  restrict_chg=1 + restrict_cur=0  USB 8.62V @ 1.60A  电池 -2.58A  <- 完全无效
  C3  battery_idle=1 (三锁齐下)      USB 8.62V @ 1.60A   电池 -2.50A  <- 反而失效

  满载(8 线程)旁路验证: USB 自动升到 0.73~1.18A @8.8V (~10W), 电池 0mA(max 18mA),
                        capacity 恒 80%, 温度仅 +1.5C -> 硬件旁路完好, 无功率缺口
  night_charging 粘性: 单次写入后 40 秒无回弹 -> 无需任何 workqueue 保活

因此本补丁的设计原则:
  1. 只用 night_charging 这一个真实有效的通道 (ADSP 的 fcc_NightVoterC=0)
  2. 不用 restrict_chg / restrict_cur  (无效, 且 restrict_chg=1 + night=0 会伪装成
     "Not charging" 实际 2.6A 满速充电 = 用户报告的那个假旁路 bug)
  3. 不用 workqueue 保活 (粘性, 且每 3 秒一次 1 秒超时的 GLink RPC 反而是隐患)
  4. 不伪造 CONSTANT_CHARGE_CURRENT / CHARGE_CONTROL_LIMIT (ADSP 本来就忽略 AP 写入,
     旁路时 CURRENT_NOW 硬件天然上报 0, 伪造只会掩盖真实状态)
  5. 写入后必须回读 ADSP 真值校验, 失败则清状态并返回 -EIO, 绝不留下假旁路
  6. 仅伪造 STATUS -> "Not charging" (ACC/Scene 靠它判定 battIdleMode), 且
     只在硬件已确认处于旁路时伪造
  7. 属性组收窄到 type==BATTERY 的 psy, 不再污染 usb/ 和 wireless/
"""

import sys
import os

target_file = "drivers/power/supply/power_supply_sysfs.c"

SENTINEL = "XM_BYPASS_V2_MARKER"

header = r'''
/* ===== [XM_BYPASS_V2_MARKER] Xiaomi hardware bypass charging ===== */
#include <linux/fs.h>
#include <linux/err.h>
#include <linux/jiffies.h>
#include <linux/delay.h>
#include <linux/string.h>

#define XM_NIGHT_PATH		"/sys/class/qcom-battery/night_charging"
#define XM_RESTRICT_CHG_PATH	"/sys/class/qcom-battery/restrict_chg"

/* 旁路激活标志。仅在硬件回读确认后置 1，绝不乐观置位 */
static int g_battery_idle_state;
/* 上次核对硬件真实状态的时间，用于节流健康检查 */
static unsigned long g_last_verify;

static int xm_sysfs_write_str(const char *path, const char *val)
{
	struct file *f;
	loff_t pos = 0;
	ssize_t ret;

	f = filp_open(path, O_WRONLY, 0);
	if (IS_ERR(f))
		return (int)PTR_ERR(f);

	ret = kernel_write(f, val, strlen(val), &pos);
	filp_close(f, NULL);

	return ret < 0 ? (int)ret : 0;
}

static int xm_sysfs_read_int(const char *path, int *out)
{
	struct file *f;
	loff_t pos = 0;
	char buf[24];
	ssize_t ret;
	int val;

	f = filp_open(path, O_RDONLY, 0);
	if (IS_ERR(f))
		return (int)PTR_ERR(f);

	memset(buf, 0, sizeof(buf));
	ret = kernel_read(f, buf, sizeof(buf) - 1, &pos);
	filp_close(f, NULL);

	if (ret <= 0)
		return ret < 0 ? (int)ret : -EIO;

	if (kstrtoint(buf, 10, &val))
		return -EINVAL;

	*out = val;
	return 0;
}

/*
 * 下发旁路开关并回读校验。
 * night_charging 是唯一能穿透 ADSP 仲裁、让 fcc_NightVoterC=0 的通道；
 * 写入被 ADSP 拒绝时回读值不会变，必须靠回读才能发现。
 */
static int xm_bypass_apply(int enable)
{
	int rc, val = -1;

	rc = xm_sysfs_write_str(XM_NIGHT_PATH, enable ? "1\n" : "0\n");
	if (rc) {
		pr_err("battery_idle: night_charging write failed (%d)\n", rc);
		return rc;
	}

	rc = xm_sysfs_read_int(XM_NIGHT_PATH, &val);
	if (rc) {
		pr_err("battery_idle: night_charging readback failed (%d)\n", rc);
		return rc;
	}

	if (val != (enable ? 1 : 0)) {
		pr_err("battery_idle: night_charging mismatch, want %d got %d\n",
		       enable ? 1 : 0, val);
		return -EIO;
	}

	/*
	 * 关闭时顺手清掉旧版补丁可能残留的 restrict_chg。
	 * 单独 restrict_chg=1 本身不改变硬件行为（实测 1.6A 照充），
	 * 但它是旧版"假旁路"状态的组成部分，清理掉更干净。失败无害。
	 */
	if (!enable)
		xm_sysfs_write_str(XM_RESTRICT_CHG_PATH, "0\n");

	pr_info("battery_idle: hardware bypass %s (night_charging=%d)\n",
		enable ? "ENABLED" : "DISABLED", val);

	return 0;
}

static ssize_t battery_idle_show(struct device *dev,
				 struct device_attribute *attr,
				 char *buf)
{
	/*
	 * 健康检查（自愈）：若有别的组件（micharge HAL / 高温保护）把
	 * night_charging 关掉了，这里必须同步清零，否则 STATUS 会继续伪装成
	 * "Not charging" 而电池实际在满速充电 —— 正是最初的假旁路 bug。
	 * 2 秒节流，避免 Scene/ACC 轮询触发 GLink RPC 风暴。
	 */
	if (g_battery_idle_state &&
	    time_after(jiffies, g_last_verify + msecs_to_jiffies(2000))) {
		int hw = -1;

		g_last_verify = jiffies;
		if (xm_sysfs_read_int(XM_NIGHT_PATH, &hw) || hw != 1) {
			pr_warn("battery_idle: hardware left bypass (hw=%d), state cleared\n",
				hw);
			g_battery_idle_state = 0;
		}
	}

	return sprintf(buf, "%d\n", g_battery_idle_state);
}

static ssize_t battery_idle_store(struct device *dev,
				  struct device_attribute *attr,
				  const char *buf, size_t count)
{
	struct power_supply *psy = dev_get_drvdata(dev);
	int val, rc;

	if (kstrtoint(buf, 10, &val) < 0)
		return -EINVAL;

	if (!psy || !psy->desc || psy->desc->type != POWER_SUPPLY_TYPE_BATTERY)
		return count;

	val = !!val;

	rc = xm_bypass_apply(val);
	if (rc < 0) {
		/*
		 * 硬件没接受：状态保持关闭并上报错误。
		 * 绝对不能在硬件未生效时把 state 置 1，否则就是假旁路。
		 */
		g_battery_idle_state = 0;
		return rc;
	}

	g_battery_idle_state = val;
	g_last_verify = jiffies;
	power_supply_changed(psy);

	return count;
}

/* ACC 用 charging_enabled（1=允许充电 / 0=禁止充电），语义与 battery_idle 相反 */
static ssize_t charging_enabled_show(struct device *dev,
				     struct device_attribute *attr,
				     char *buf)
{
	return sprintf(buf, "%d\n", !g_battery_idle_state);
}

static ssize_t charging_enabled_store(struct device *dev,
				      struct device_attribute *attr,
				      const char *buf, size_t count)
{
	int val;

	if (kstrtoint(buf, 10, &val) < 0)
		return -EINVAL;

	return battery_idle_store(dev, attr, val ? "0" : "1", count);
}

static DEVICE_ATTR_RW(battery_idle);
static DEVICE_ATTR_RW(charging_enabled);

static struct attribute *battery_idle_attrs[] = {
	&dev_attr_battery_idle.attr,
	&dev_attr_charging_enabled.attr,
	NULL,
};

/*
 * 只暴露在真正的电池 psy 上。
 * 旧版把属性组全局挂进 power_supply_attr_groups[]，导致 usb/battery_idle、
 * wireless/charging_enabled 等误导性节点出现，会带偏 Scene/ACC 的探测顺序。
 */
static umode_t battery_idle_attr_visible(struct kobject *kobj,
					 struct attribute *attr, int n)
{
	struct device *dev = kobj_to_dev(kobj);
	struct power_supply *psy = dev_get_drvdata(dev);

	if (!psy || !psy->desc || psy->desc->type != POWER_SUPPLY_TYPE_BATTERY)
		return 0;

	return attr->mode;
}

static const struct attribute_group battery_idle_attr_group = {
	.attrs = battery_idle_attrs,
	.is_visible = battery_idle_attr_visible,
};
/* ===== end [XM_BYPASS_V2_MARKER] ===== */
'''

status_block = '''	/*
	 * 旁路激活时向用户态报告 "Not charging"，ACC/Scene 靠它判定 battIdleMode。
	 * 仅在硬件已确认处于旁路（g_battery_idle_state 由回读校验置位）时伪造，
	 * 其余属性全部透传 ADSP 真值，不做任何伪装。
	 */
	if (g_battery_idle_state && psy && psy->desc &&
	    psy->desc->type == POWER_SUPPLY_TYPE_BATTERY &&
	    psp == POWER_SUPPLY_PROP_STATUS)
		return sprintf(buf, "Not charging\\n");

	if (psp == POWER_SUPPLY_PROP_TYPE) {'''


def main():
    if not os.path.exists(target_file):
        print(f"Error: {target_file} not found in {os.getcwd()}")
        sys.exit(1)

    with open(target_file, "r") as f:
        content = f.read()

    if SENTINEL in content:
        print(f"Already patched ({SENTINEL} present), skipping.")
        return

    # --- 1. 注入头部实现 ---
    marker_show = 'static ssize_t power_supply_show_property('
    assert marker_show in content, "Marker show_property not found!"
    content = content.replace(marker_show, header + '\n' + marker_show, 1)

    # --- 2. STATUS 伪装（唯一保留的展示层改写） ---
    marker_type = 'if (psp == POWER_SUPPLY_PROP_TYPE) {'
    assert content.count(marker_type) == 1, "Marker TYPE not unique!"
    content = content.replace(marker_type, status_block, 1)

    # --- 3. 注册属性组 ---
    marker_group = '''static const struct attribute_group *power_supply_attr_groups[] = {
	&power_supply_attr_group,
	NULL,
};'''
    assert marker_group in content, "Marker attribute_groups not found!"
    content = content.replace(
        marker_group,
        '''static const struct attribute_group *power_supply_attr_groups[] = {
	&power_supply_attr_group,
	&battery_idle_attr_group,
	NULL,
};''', 1)

    with open(target_file, "w") as f:
        f.write(content)

    print(f"[OK] Xiaomi hardware bypass (v2) injected into {target_file}")
    print("     - channel      : night_charging only (readback verified)")
    print("     - workqueue    : none (night_charging is sticky)")
    print("     - restrict_*   : not used / cleaned up on disable")
    print("     - faked props  : STATUS only, gated on verified hw state")
    print("     - attr scope   : POWER_SUPPLY_TYPE_BATTERY only")


if __name__ == "__main__":
    main()
