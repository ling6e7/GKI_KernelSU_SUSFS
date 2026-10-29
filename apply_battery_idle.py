#!/usr/bin/env python3
import sys, os

target_file = "drivers/power/supply/power_supply_sysfs.c"
if not os.path.exists(target_file):
    print(f"Error: {target_file} not found in {os.getcwd()}")
    sys.exit(1)

with open(target_file, "r") as f:
    content = f.read()

header = '''
#include <linux/fs.h>
#include <linux/err.h>
#include <linux/workqueue.h>
#include <linux/jiffies.h>

static int g_battery_idle_state = 0;
static int g_saved_fcc = -1;

static void xm_write_sysfs(const char *path, const char *val, size_t len)
{
	struct file *f;
	loff_t pos = 0;

	f = filp_open(path, O_WRONLY, 0);
	if (!IS_ERR(f)) {
		kernel_write(f, val, len, &pos);
		filp_close(f, NULL);
	}
}

static void xm_set_hardware_bypass(int enable)
{
	if (enable) {
		/*
		 * 硬件真旁路四重锁死（适配高通 ADSP / 小米 8+ Gen1 平台）：
		 * 1. night_charging = 1 (激活 ADSP 夜间充电保护 0mA 投票器)
		 * 2. restrict_chg = 1   (激活 ADSP 驻店限制充电模式)
		 * 3. restrict_cur = 0   (将驻店限制充电电流上限硬锁为 0 uA，彻底突破 80% 固件门限)
		 */
		xm_write_sysfs("/sys/class/qcom-battery/night_charging", "1\\n", 2);
		xm_write_sysfs("/sys/class/qcom-battery/restrict_chg", "1\\n", 2);
		xm_write_sysfs("/sys/class/qcom-battery/restrict_cur", "0\\n", 2);
	} else {
		/* 恢复正常快充与充电模式 */
		xm_write_sysfs("/sys/class/qcom-battery/night_charging", "0\\n", 2);
		xm_write_sysfs("/sys/class/qcom-battery/restrict_chg", "0\\n", 2);
		xm_write_sysfs("/sys/class/qcom-battery/restrict_cur", "1000000\\n", 8);
	}
}

static void battery_idle_work_fn(struct work_struct *work);
static DECLARE_DELAYED_WORK(g_battery_idle_work, battery_idle_work_fn);

static void battery_idle_work_fn(struct work_struct *work)
{
	if (g_battery_idle_state) {
		xm_set_hardware_bypass(1);
		schedule_delayed_work(&g_battery_idle_work, msecs_to_jiffies(3000));
	}
}

static ssize_t battery_idle_show(struct device *dev,
				 struct device_attribute *attr,
				 char *buf)
{
	return sprintf(buf, "%d\\n", g_battery_idle_state);
}

static ssize_t battery_idle_store(struct device *dev,
				  struct device_attribute *attr,
				  const char *buf, size_t count)
{
	struct power_supply *psy = dev_get_drvdata(dev);
	int val;

	if (kstrtoint(buf, 10, &val) < 0)
		return -EINVAL;

	if (psy && psy->desc && psy->desc->type == POWER_SUPPLY_TYPE_BATTERY) {
		union power_supply_propval pval;

		if (val) {
			/* 开启硬件真旁路供电：硬锁底层限流器、关闭快充/电荷泵，下发 0 uA */
			g_battery_idle_state = 1;
			xm_set_hardware_bypass(1);
			if (psy->desc->get_property &&
			    psy->desc->get_property(psy, POWER_SUPPLY_PROP_CONSTANT_CHARGE_CURRENT, &pval) == 0) {
				if (pval.intval > 0)
					g_saved_fcc = pval.intval;
			}
			pval.intval = 0;
			if (psy->desc->set_property)
				psy->desc->set_property(psy, POWER_SUPPLY_PROP_CONSTANT_CHARGE_CURRENT, &pval);
			schedule_delayed_work(&g_battery_idle_work, msecs_to_jiffies(3000));
		} else {
			/* 关闭硬件真旁路供电：停止保活守护，恢复快充与原快充电流 */
			g_battery_idle_state = 0;
			cancel_delayed_work_sync(&g_battery_idle_work);
			xm_set_hardware_bypass(0);
			pval.intval = (g_saved_fcc > 0) ? g_saved_fcc : -1;
			if (psy->desc->set_property)
				psy->desc->set_property(psy, POWER_SUPPLY_PROP_CONSTANT_CHARGE_CURRENT, &pval);
		}
		power_supply_changed(psy);
	}

	return count;
}

static ssize_t charging_enabled_show(struct device *dev,
				     struct device_attribute *attr,
				     char *buf)
{
	return sprintf(buf, "%d\\n", !g_battery_idle_state);
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

static const struct attribute_group battery_idle_attr_group = {
	.attrs = battery_idle_attrs,
};
'''

thermal_block = '''
	/*
	 * 硬件真旁路防护：当旁路模式激活时，阻止外部 mi_thermald / powerkeeper 等守护进程
	 * 周期性覆写 CHARGE_CONTROL_LIMIT 或 CONSTANT_CHARGE_CURRENT 重新唤醒大电流！
	 */
	if (g_battery_idle_state && psy && psy->desc && psy->desc->type == POWER_SUPPLY_TYPE_BATTERY) {
		if (psp == POWER_SUPPLY_PROP_CHARGE_CONTROL_LIMIT ||
		    psp == POWER_SUPPLY_PROP_CONSTANT_CHARGE_CURRENT) {
			return count;
		}
	}
'''

visible_patch = '''			if (property == POWER_SUPPLY_PROP_CONSTANT_CHARGE_CURRENT)
				mode |= S_IWUSR;
'''

assert 'static ssize_t power_supply_show_property(' in content, "Marker show_property not found!"
content = content.replace('static ssize_t power_supply_show_property(', header + '\nstatic ssize_t power_supply_show_property(')

target_status_show = 'if (psp == POWER_SUPPLY_PROP_TYPE) {'
replacement_status_show = '''if (g_battery_idle_state && psy && psy->desc && psy->desc->type == POWER_SUPPLY_TYPE_BATTERY) {
		if (psp == POWER_SUPPLY_PROP_STATUS)
			return sprintf(buf, "Not charging\\n");
		if (psp == POWER_SUPPLY_PROP_CONSTANT_CHARGE_CURRENT)
			return sprintf(buf, "0\\n");
		if (psp == POWER_SUPPLY_PROP_CHARGE_CONTROL_LIMIT)
			return sprintf(buf, "0\\n");
	}

	if (psp == POWER_SUPPLY_PROP_TYPE) {'''
assert target_status_show in content, "Marker target_status_show not found!"
content = content.replace(target_status_show, replacement_status_show)

target_store = 'value.intval = ret;\n\n\tret = power_supply_set_property(psy, psp, &value);'
replacement_store = 'value.intval = ret;\n' + thermal_block + '\n\tret = power_supply_set_property(psy, psp, &value);'
assert target_store in content, "Marker target_store not found!"
content = content.replace(target_store, replacement_store)

target_visible = 'if (psy->desc->property_is_writeable &&\n\t\t\t    psy->desc->property_is_writeable(psy, property) > 0)\n\t\t\t\tmode |= S_IWUSR;\n\n\t\t\treturn mode;'
replacement_visible = 'if (psy->desc->property_is_writeable &&\n\t\t\t    psy->desc->property_is_writeable(psy, property) > 0)\n\t\t\t\tmode |= S_IWUSR;\n\n' + visible_patch + '\n\t\t\treturn mode;'
assert target_visible in content, "Marker target_visible not found!"
content = content.replace(target_visible, replacement_visible)

target_group = '''static const struct attribute_group *power_supply_attr_groups[] = {
	&power_supply_attr_group,
	NULL,
};'''
replacement_group = '''static const struct attribute_group *power_supply_attr_groups[] = {
	&power_supply_attr_group,
	&battery_idle_attr_group,
	NULL,
};'''
assert target_group in content, "Marker attribute_groups not found!"
content = content.replace(target_group, replacement_group)

with open(target_file, "w") as f:
    f.write(content)

print(f"Successfully injected True Battery Idle hardware patch into {target_file}!")
