// SPDX-License-Identifier: GPL-2.0-only
/*
 * xm_probe - standalone PMIC glink battery-charger property probe
 *
 * Purpose: the vendor driver (qti_battery_charger_main_m81) exposes only ~40
 * of the 159 ADSP XM properties through sysfs, and it owns the PMIC glink
 * client id MSG_OWNER_BC (32778) exclusively (pmic_glink uses idr_alloc with a
 * single-slot range). This module registers its *own*, distinct client id so
 * it can send BC_XM_STATUS_GET/SET for ANY property id without touching the
 * stock driver, then be rmmod'ed cleanly (nothing depends on it).
 *
 * Everything is driven from debugfs:
 *   /sys/kernel/debug/xm_probe/cmd    write "G <id>" / "S <id> <val>"
 *   /sys/kernel/debug/xm_probe/scan   write "<start> <count>", then read it
 *   /sys/kernel/debug/xm_probe/last   read the last result
 *   /sys/kernel/debug/xm_probe/info   read registration state
 */

#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/platform_device.h>
#include <linux/debugfs.h>
#include <linux/seq_file.h>
#include <linux/mutex.h>
#include <linux/completion.h>
#include <linux/delay.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <linux/string.h>
#include <linux/device.h>
#include <linux/soc/qcom/pmic_glink.h>

#define MSG_TYPE_REQ_RESP		1

#define BC_BATTERY_STATUS_GET		0x30
#define BC_BATTERY_STATUS_SET		0x31
#define BC_USB_STATUS_GET		0x32
#define BC_USB_STATUS_SET		0x33
#define BC_WLS_STATUS_GET		0x34
#define BC_WLS_STATUS_SET		0x35
#define BC_XM_STATUS_GET		0x50
#define BC_XM_STATUS_SET		0x51

#define BC_WAIT_TIME_MS			1000
#define XM_PROP_MAX			159

struct bc_req_msg {
	struct pmic_glink_hdr	hdr;
	u32			battery_id;
	u32			property_id;
	u32			value;
};

struct bc_resp_msg {
	struct pmic_glink_hdr	hdr;
	u32			property_id;
	u32			value;
	u32			ret_code;
};

struct xm_probe_dev {
	struct device			*dev;
	struct pmic_glink_client	*client;
	struct mutex			lock;		/* serialises tx/rx */
	struct completion		ack;
	u32				last_prop;
	u32				last_value;
	int				last_ret;
	u32				opcode_get;
	u32				opcode_set;
	struct dentry			*debugfs_dir;
	char				last_line[128];
};

static int owner_id = 32790;
module_param(owner_id, int, 0444);
MODULE_PARM_DESC(owner_id, "PMIC glink client id to register with (default 32790)");

/* opcode_get / opcode_set are settable at runtime so both BC and XM channels
 * can be exercised without rebuilding.
 */
static int op_get = BC_XM_STATUS_GET;
module_param(op_get, int, 0644);
MODULE_PARM_DESC(op_get, "opcode used for 'G' (default 0x50 = BC_XM_STATUS_GET)");

static int op_set = BC_XM_STATUS_SET;
module_param(op_set, int, 0644);
MODULE_PARM_DESC(op_set, "opcode used for 'S' (default 0x51 = BC_XM_STATUS_SET)");

static int battery_id = 0;
module_param(battery_id, int, 0644);
MODULE_PARM_DESC(battery_id, "battery_id field in requests (default 0)");

static void xm_probe_state_cb(void *priv, enum pmic_glink_state state)
{
	pr_info("xm_probe: pmic_glink state -> %d\n", state);
}

static int xm_probe_msg_cb(void *priv, void *data, size_t len)
{
	struct xm_probe_dev *xp = priv;
	struct bc_resp_msg *resp = data;

	if (len < sizeof(*resp)) {
		pr_warn("xm_probe: short response %zu bytes\n", len);
		return 0;
	}

	pr_info("xm_probe: resp opcode=0x%x prop=%u value=%u rc=%d\n",
		resp->hdr.opcode, resp->property_id, resp->value,
		(int)resp->ret_code);

	if (resp->hdr.opcode == xp->opcode_get ||
	    resp->hdr.opcode == xp->opcode_set) {
		xp->last_prop = resp->property_id;
		xp->last_value = resp->value;
		xp->last_ret = (int)resp->ret_code;
		complete(&xp->ack);
	}

	return 0;
}

static int xm_probe_tx(struct xm_probe_dev *xp, u32 opcode, u32 prop, u32 val,
		       u32 *out_val, int *out_rc)
{
	struct bc_req_msg req = { { 0 } };
	unsigned long left;
	int rc;

	req.hdr.owner = owner_id;
	req.hdr.type = MSG_TYPE_REQ_RESP;
	req.hdr.opcode = opcode;
	req.battery_id = battery_id;
	req.property_id = prop;
	req.value = val;

	mutex_lock(&xp->lock);
	reinit_completion(&xp->ack);
	xp->last_prop = ~0u;
	xp->last_value = 0;
	xp->last_ret = -999;

	rc = pmic_glink_write(xp->client, &req, sizeof(req));
	if (rc) {
		mutex_unlock(&xp->lock);
		pr_err("xm_probe: pmic_glink_write failed rc=%d\n", rc);
		return rc;
	}

	left = wait_for_completion_timeout(&xp->ack,
					   msecs_to_jiffies(BC_WAIT_TIME_MS));
	if (!left) {
		mutex_unlock(&xp->lock);
		pr_err("xm_probe: timeout opcode=0x%x prop=%u\n", opcode, prop);
		return -ETIMEDOUT;
	}

	if (out_val)
		*out_val = xp->last_value;
	if (out_rc)
		*out_rc = xp->last_ret;
	mutex_unlock(&xp->lock);

	return 0;
}

static int xm_probe_last_show(struct seq_file *s, void *unused)
{
	struct xm_probe_dev *xp = s->private;

	seq_printf(s, "%s\n", xp->last_line[0] ? xp->last_line : "(no result yet)");
	return 0;
}

static int xm_probe_last_open(struct inode *inode, struct file *file)
{
	return single_open(file, xm_probe_last_show, inode->i_private);
}

static const struct file_operations xm_probe_last_fops = {
	.owner	 = THIS_MODULE,
	.open	 = xm_probe_last_open,
	.read	 = seq_read,
	.llseek	 = seq_lseek,
	.release = single_release,
};

static int xm_probe_info_show(struct seq_file *s, void *unused)
{
	struct xm_probe_dev *xp = s->private;

	seq_printf(s, "owner_id   = %d (0x%x)\n", owner_id, owner_id);
	seq_printf(s, "op_get     = 0x%x\n", op_get);
	seq_printf(s, "op_set     = 0x%x\n", op_set);
	seq_printf(s, "battery_id = %d\n", battery_id);
	seq_printf(s, "client     = %s\n", xp->client ? "registered" : "none");
	seq_printf(s, "xm_prop_max= %d\n", XM_PROP_MAX);
	return 0;
}

static int xm_probe_info_open(struct inode *inode, struct file *file)
{
	return single_open(file, xm_probe_info_show, inode->i_private);
}

static const struct file_operations xm_probe_info_fops = {
	.owner	 = THIS_MODULE,
	.open	 = xm_probe_info_open,
	.read	 = seq_read,
	.llseek	 = seq_lseek,
	.release = single_release,
};

/* --- cmd: "G <id>" or "S <id> <val>" ------------------------------------- */
static ssize_t xm_probe_cmd_write(struct file *file, const char __user *ubuf,
				  size_t count, loff_t *ppos)
{
	struct xm_probe_dev *xp = file->private_data;
	unsigned int id = 0, val = 0;
	char buf[64], op;
	int n, rc, sret = 0;
	u32 out = 0;

	if (count == 0 || count >= sizeof(buf))
		return -EINVAL;
	if (copy_from_user(buf, ubuf, count))
		return -EFAULT;
	buf[count] = '\0';

	n = sscanf(buf, " %c %u %u", &op, &id, &val);
	if (n < 2)
		return -EINVAL;

	rc = xm_probe_tx(xp, op_get, id, 0, &out, &sret);

	if (!rc)
		snprintf(xp->last_line, sizeof(xp->last_line),
			 "G id=%u value=%u (0x%x) rc=%d", id, out, out, sret);
	else
		snprintf(xp->last_line, sizeof(xp->last_line),
			 "G id=%u FAILED tx_rc=%d", id, rc);

	if (op == 'S' && !rc) {
		u32 prev = out;

		rc = xm_probe_tx(xp, op_set, id, val, &out, &sret);
		if (!rc)
			snprintf(xp->last_line, sizeof(xp->last_line),
				 "S id=%u wrote=%u readback=%u rc=%d (was %u)",
				 id, val, out, sret, prev);
		else
			snprintf(xp->last_line, sizeof(xp->last_line),
				 "S id=%u FAILED tx_rc=%d", id, rc);
	}

	pr_info("xm_probe: %s\n", xp->last_line);
	return count;
}

static const struct file_operations xm_probe_cmd_fops = {
	.owner	 = THIS_MODULE,
	.open	 = simple_open,
	.write	 = xm_probe_cmd_write,
	.llseek	 = no_llseek,
};

/* --- scan: write "<start> <count>", then read --------------------------- */
struct xm_probe_scan {
	u32	start;
	u32	count;
	struct xm_probe_dev *xp;
};

static int xm_probe_scan_show(struct seq_file *s, void *unused)
{
	struct xm_probe_scan *sc = s->private;
	struct xm_probe_dev *xp = sc->xp;
	u32 i, end;
	u32 v;
	int rc, sret;
	int ok = 0, bad = 0;

	end = sc->start + sc->count;
	if (end > XM_PROP_MAX)
		end = XM_PROP_MAX;

	seq_printf(s, "# opcode_get=0x%x owner=%d range %u..%u\n",
		   op_get, owner_id, sc->start, end);

	for (i = sc->start; i < end; i++) {
		rc = xm_probe_tx(xp, op_get, i, 0, &v, &sret);
		if (rc) {
			seq_printf(s, "%3u rc=tx-%d\n", i, rc);
			bad++;
			continue;
		}
		seq_printf(s, "%3u %11u (0x%08x) rc=%d\n", i, v, v, sret);
		ok++;
	}

	seq_printf(s, "# done: ok=%d fail=%d\n", ok, bad);
	return 0;
}

static int xm_probe_scan_open(struct inode *inode, struct file *file)
{
	return single_open(file, xm_probe_scan_show, inode->i_private);
}

static ssize_t xm_probe_scan_write(struct file *file, const char __user *ubuf,
				   size_t count, loff_t *ppos)
{
	struct seq_file *sf = file->private_data;
	struct xm_probe_scan *sc = sf->private;
	char buf[64];
	unsigned int a = 0, b = 0;

	if (count == 0 || count >= sizeof(buf))
		return -EINVAL;
	if (copy_from_user(buf, ubuf, count))
		return -EFAULT;
	buf[count] = '\0';

	if (sscanf(buf, "%u %u", &a, &b) != 2)
		return -EINVAL;

	sc->start = a;
	sc->count = b ? b : 1;
	return count;
}

static const struct file_operations xm_probe_scan_fops = {
	.owner	 = THIS_MODULE,
	.open	 = xm_probe_scan_open,
	.read	 = seq_read,
	.write	 = xm_probe_scan_write,
	.llseek	 = seq_lseek,
	.release = single_release,
};

static int xm_core_probe(struct platform_device *pdev)
{
	struct xm_probe_dev *xp;
	struct pmic_glink_client_data cdata = { 0 };

	xp = devm_kzalloc(&pdev->dev, sizeof(*xp), GFP_KERNEL);
	if (!xp)
		return -ENOMEM;

	xp->dev = &pdev->dev;
	mutex_init(&xp->lock);
	init_completion(&xp->ack);
	platform_set_drvdata(pdev, xp);

	cdata.name = "xm_probe";
	cdata.id = owner_id;
	cdata.priv = xp;
	cdata.msg_cb = xm_probe_msg_cb;
	cdata.state_cb = xm_probe_state_cb;

	xp->client = pmic_glink_register_client(&pdev->dev, &cdata);
	if (IS_ERR(xp->client)) {
		int rc = PTR_ERR(xp->client);

		dev_err(&pdev->dev,
			"pmic_glink_register_client(id=%d) failed rc=%d\n",
			owner_id, rc);
		xp->client = NULL;
		return rc;
	}

	dev_info(&pdev->dev, "registered pmic_glink client id=%d\n", owner_id);

	xp->debugfs_dir = debugfs_create_dir("xm_probe", NULL);
	if (IS_ERR_OR_NULL(xp->debugfs_dir)) {
		xp->debugfs_dir = NULL;
		dev_warn(&pdev->dev, "debugfs unavailable\n");
	} else {
		struct xm_probe_scan *sc;

		debugfs_create_file("cmd", 0200, xp->debugfs_dir, xp,
				    &xm_probe_cmd_fops);
		debugfs_create_file("last", 0400, xp->debugfs_dir, xp,
				    &xm_probe_last_fops);
		debugfs_create_file("info", 0400, xp->debugfs_dir, xp,
				    &xm_probe_info_fops);

		sc = devm_kzalloc(&pdev->dev, sizeof(*sc), GFP_KERNEL);
		if (sc) {
			sc->start = 0;
			sc->count = XM_PROP_MAX;
			sc->xp = xp;
			debugfs_create_file("scan", 0600, xp->debugfs_dir, sc,
					    &xm_probe_scan_fops);
		}
	}

	return 0;
}

static int xm_core_remove(struct platform_device *pdev)
{
	struct xm_probe_dev *xp = platform_get_drvdata(pdev);

	if (xp->debugfs_dir)
		debugfs_remove_recursive(xp->debugfs_dir);
	if (xp->client)
		pmic_glink_unregister_client(xp->client);
	dev_info(&pdev->dev, "unregistered\n");
	return 0;
}

static struct platform_driver xm_core_driver = {
	.probe	= xm_core_probe,
	.remove	= xm_core_remove,
	.driver	= {
		.name = "xm_probe",
	},
};

static struct platform_device *xm_core_pdev;

static int __init xm_probe_init(void)
{
	struct device *pmic_dev;
	int rc;

	/*
	 * pmic_glink_register_client() resolves the glink device through
	 * dev->parent, so our platform device must be a child of
	 * soc:qcom,pmic_glink.
	 */
	pmic_dev = bus_find_device_by_name(&platform_bus_type, NULL,
					   "soc:qcom,pmic_glink");
	if (!pmic_dev) {
		pr_err("xm_probe: pmic_glink platform device not found\n");
		return -ENODEV;
	}

	xm_core_pdev = platform_device_register_data(pmic_dev, "xm_probe",
						     PLATFORM_DEVID_AUTO,
						     NULL, 0);
	put_device(pmic_dev);
	if (IS_ERR(xm_core_pdev)) {
		rc = PTR_ERR(xm_core_pdev);
		pr_err("xm_probe: platform_device_register_data rc=%d\n", rc);
		xm_core_pdev = NULL;
		return rc;
	}

	rc = platform_driver_register(&xm_core_driver);
	if (rc) {
		pr_err("xm_probe: platform_driver_register rc=%d\n", rc);
		platform_device_unregister(xm_core_pdev);
		xm_core_pdev = NULL;
		return rc;
	}

	pr_info("xm_probe: loaded\n");
	return 0;
}

static void __exit xm_probe_exit(void)
{
	platform_driver_unregister(&xm_core_driver);
	if (xm_core_pdev)
		platform_device_unregister(xm_core_pdev);
	pr_info("xm_probe: unloaded\n");
}

module_init(xm_probe_init);
module_exit(xm_probe_exit);

MODULE_DESCRIPTION("PMIC glink battery-charger XM property probe");
MODULE_LICENSE("GPL v2");
