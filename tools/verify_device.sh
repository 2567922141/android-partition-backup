#!/system/bin/sh
echo "=== 环境完好性复验 ==="
echo "slot        : $(getprop ro.boot.slot_suffix)"
echo "system      : $(getprop ro.build.version.incremental)"
echo "model       : $(getprop ro.product.model)"
echo "root        : $(id)"
echo -n "BL real     : "; grep -o 'verifiedbootstate = "[a-z]*"' /proc/bootconfig
echo -n "devstate    : "; grep -o 'vbmeta.device_state = "[a-z]*"' /proc/bootconfig
echo "modules     : $(ls -1 /data/adb/modules 2>/dev/null | wc -l)"
echo "by-name     : $(ls -1 /dev/block/by-name 2>/dev/null | wc -l)"
echo -n "persist     : "; mount | grep -c 'on /mnt/vendor/persist'
echo ""
echo "=== 本次工具在设备端留下的东西（应为空）==="
ls -d /sdcard/.apb_tmp 2>/dev/null && echo "  !! .apb_tmp 残留" || echo "  ✅ 无 .apb_tmp"
ls -l /data/local/tmp/.apb_probe.sh 2>/dev/null && echo "  !! 探针残留" || echo "  ✅ 无探针脚本"
ls /sdcard/*.img 2>/dev/null && echo "  !! sdcard 有 img" || echo "  ✅ sdcard 无残留 img"
echo ""
echo "=== /data/local/tmp 文件数（本次之前就是 16）==="
ls -1 /data/local/tmp | wc -l
echo "=== DONE ==="
