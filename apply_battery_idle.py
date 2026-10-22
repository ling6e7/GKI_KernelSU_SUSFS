#!/usr/bin/env python3
import sys, os

target_file = "drivers/power/supply/power_supply_sysfs.c"
if not os.path.exists(target_file):
    print(f"Error: {target_file} not found in {os.getcwd()}")
    sys.exit(1)

with open(target_file, "r") as f:
    content = f.read()

header = '''
static int g_battery_idle_state = 0;

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

	g_battery_idle_state = !!val;
	if (psy)
		power_supply_changed(psy);
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
	struct power_supply *psy = dev_get_drvdata(dev);
	int ret;

	if (kstrtoint(buf, 10, &ret) < 0)
		return -EINVAL;

	g_battery_idle_state = !ret;
	if (psy)
		power_supply_changed(psy);
	return count;
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

intercept = '''
	if (g_battery_idle_state && psy && psy->desc && psy->desc->type == POWER_SUPPLY_TYPE_BATTERY) {
		if (psp == POWER_SUPPLY_PROP_STATUS)
			return sprintf(buf, "Not charging\\n");
		if (psp == POWER_SUPPLY_PROP_CURRENT_NOW)
			return sprintf(buf, "0\\n");
	}
'''

assert 'static ssize_t power_supply_show_property(' in content, "Marker show_property not found!"
content = content.replace('static ssize_t power_supply_show_property(', header + '\nstatic ssize_t power_supply_show_property(')

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

assert 'if (psp == POWER_SUPPLY_PROP_TYPE) {' in content, "Marker psp == PROP_TYPE not found!"
content = content.replace('if (psp == POWER_SUPPLY_PROP_TYPE) {', intercept + '\n\tif (psp == POWER_SUPPLY_PROP_TYPE) {')

with open(target_file, "w") as f:
    f.write(content)

print(f"Successfully injected Battery Idle sysfs patch into {target_file}!")
