import os
import sys
import time
import json
import traceback
import subprocess
import shutil
import getpass
from google.cloud import compute_v1
from google.api_core import exceptions

# 常量定义
MAX_RETRIES = 60  # CPU检测最大重试次数（2分钟）
FIREWALL_RULES_TO_CLEAN = ["allow-all-ingress-custom", "deny-cdn-egress-custom"]
REMOTE_SCRIPT_URLS = {
    "apt": "https://raw.githubusercontent.com/xxx/debian-apt/main/apt.sh",  # 替换为实际换源脚本地址
    "dae": "https://raw.githubusercontent.com/xxx/dae-install/main/install.sh",  # 替换为实际dae安装脚本地址
    "net_iptables": "https://raw.githubusercontent.com/xxx/traffic-monitor/main/net_iptables.sh",  # 替换实际地址
    "net_shutdown": "https://raw.githubusercontent.com/xxx/traffic-monitor/main/net_shutdown.sh",  # 替换实际地址
}

# 颜色输出函数
def print_info(msg):
    print(f"\033[34m[信息] {msg}\033[0m")

def print_success(msg):
    print(f"\033[32m[成功] {msg}\033[0m")

def print_warning(msg):
    print(f"\033[33m[警告] {msg}\033[0m")

def print_error(msg):
    print(f"\033[31m[错误] {msg}\033[0m")

# 等待GCP操作完成
def wait_for_operation(project_id, zone, operation_name):
    operation_client = compute_v1.ZoneOperationsClient()
    while True:
        operation = operation_client.get(project=project_id, zone=zone, operation=operation_name)
        if operation.status == compute_v1.Operation.Status.DONE:
            if operation.error:
                raise exceptions.GoogleAPICallError(f"操作失败: {operation.error}")
            return
        time.sleep(2)

# 选择GCP项目
def select_gcp_project():
    try:
        # 尝试从gcloud配置获取默认项目
        result = subprocess.run(
            ["gcloud", "config", "get-value", "project"],
            capture_output=True,
            text=True,
            check=True
        )
        default_project = result.stdout.strip()
        if default_project:
            choice = input(f"使用默认项目 {default_project}? (Y/n): ").strip().lower()
            if choice in ("", "y"):
                return default_project
        
        # 列出所有项目
        print("\n正在获取可用项目列表...")
        project_client = compute_v1.ProjectsClient()
        projects = project_client.list()
        project_list = [p.project_id for p in projects if p.project_id]
        
        if not project_list:
            print_error("未找到任何GCP项目，请先配置gcloud认证")
            sys.exit(1)
        
        print("\n可用项目:")
        for idx, proj in enumerate(project_list, 1):
            print(f"[{idx}] {proj}")
        
        while True:
            choice = input("请选择项目编号: ").strip()
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(project_list):
                    return project_list[idx]
                print_error("编号超出范围")
            except ValueError:
                print_error("请输入有效数字")
    except Exception as e:
        print_error(f"获取项目失败: {e}")
        sys.exit(1)

# 选择可用区
def select_zone(project_id):
    zone_client = compute_v1.ZonesClient()
    print("\n正在获取可用区列表（筛选UP状态）...")
    zones = []
    for zone in zone_client.list(project=project_id):
        if zone.status == compute_v1.Zone.Status.UP:
            zones.append(zone.name)
    
    if not zones:
        print_error("未找到可用的区域")
        sys.exit(1)
    
    # 优先显示us-central1系列（免费实例常用区）
    preferred_zones = [z for z in zones if "us-central1" in z]
    other_zones = [z for z in zones if z not in preferred_zones]
    display_zones = preferred_zones + other_zones
    
    print("\n可用区域 (优先显示us-central1):")
    for idx, zone in enumerate(display_zones, 1):
        print(f"[{idx}] {zone}")
    
    while True:
        choice = input("请选择区域编号: ").strip()
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(display_zones):
                return display_zones[idx]
            print_error("编号超出范围")
        except ValueError:
            print_error("请输入有效数字")

# 选择操作系统镜像
def select_os_image():
    print("\n支持的操作系统:")
    print("[1] Debian 12 (bookworm)")
    print("[2] Ubuntu 22.04 LTS")
    while True:
        choice = input("请选择操作系统 (默认1): ").strip() or "1"
        if choice == "1":
            return {
                "family": "debian-12",
                "project": "debian-cloud",
                "name": "debian-12-bookworm-v20240515"
            }
        elif choice == "2":
            return {
                "family": "ubuntu-2204-lts",
                "project": "ubuntu-os-cloud",
                "name": "ubuntu-2204-jammy-v20240515"
            }
        print_error("无效选择，请输入1或2")

# 创建GCP实例
def create_instance(project_id, zone, os_config):
    instance_name = f"free-instance-{int(time.time())}"
    print_info(f"正在创建实例: {instance_name} (区域: {zone})")
    
    instance_client = compute_v1.InstancesClient()
    disk_client = compute_v1.DisksClient()
    
    # 创建引导磁盘
    disk = compute_v1.AttachedDisk()
    initialize_params = compute_v1.AttachedDiskInitializeParams()
    initialize_params.source_image = f"projects/{os_config['project']}/global/images/family/{os_config['family']}"
    initialize_params.disk_size_gb = 10
    initialize_params.disk_type = f"zones/{zone}/diskTypes/pd-standard"
    disk.initialize_params = initialize_params
    disk.auto_delete = True
    disk.boot = True
    
    # 网络配置
    network_interface = compute_v1.NetworkInterface()
    network_interface.name = "global/networks/default"
    access_config = compute_v1.AccessConfig()
    access_config.type_ = compute_v1.AccessConfig.Type.ONE_TO_ONE_NAT
    access_config.name = "External NAT"
    network_interface.access_configs = [access_config]
    
    # 机器类型（免费层级）
    machine_type = f"zones/{zone}/machineTypes/e2-micro"
    
    # 实例配置
    instance = compute_v1.Instance()
    instance.name = instance_name
    instance.disks = [disk]
    instance.machine_type = machine_type
    instance.network_interfaces = [network_interface]
    
    # 设置元数据（启用SSH）
    instance.metadata = compute_v1.Metadata()
    instance.metadata.items = [
        compute_v1.Items(key="enable-oslogin", value="false"),
    ]
    
    try:
        operation = instance_client.insert(
            project=project_id,
            zone=zone,
            instance_resource=instance
        )
        print_info("实例创建中，请等待...")
        wait_for_operation(project_id, zone, operation.name)
        print_success(f"实例创建成功: {instance_name}")
        
        # 获取实例信息
        inst = instance_client.get(project=project_id, zone=zone, instance=instance_name)
        external_ip = None
        for interface in inst.network_interfaces:
            for config in interface.access_configs:
                if config.nat_ip:
                    external_ip = config.nat_ip
                    break
        
        if external_ip:
            print_success(f"实例外网IP: {external_ip}")
        return instance_name
    except Exception as e:
        print_error(f"创建实例失败: {e}")
        traceback.print_exc()
        return None

# 选择实例
def select_instance(project_id):
    instance_client = compute_v1.InstancesClient()
    instances = []
    zones = [z.name for z in compute_v1.ZonesClient().list(project=project_id) if z.status == compute_v1.Zone.Status.UP]
    
    print("\n正在获取所有实例...")
    for zone in zones:
        try:
            zone_instances = instance_client.list(project=project_id, zone=zone)
            for inst in zone_instances:
                # 获取外网IP
                external_ip = "-"
                for interface in inst.network_interfaces:
                    for config in interface.access_configs:
                        if config.nat_ip:
                            external_ip = config.nat_ip
                            break
                
                instances.append({
                    "name": inst.name,
                    "zone": zone,
                    "status": inst.status,
                    "external_ip": external_ip,
                    "network": inst.network_interfaces[0].network if inst.network_interfaces else "global/networks/default"
                })
        except Exception:
            continue
    
    if not instances:
        print_error("未找到任何实例")
        return None
    
    print("\n可用实例:")
    for idx, inst in enumerate(instances, 1):
        print(f"[{idx}] {inst['name']} | {inst['zone']} | 状态: {inst['status']} | IP: {inst['external_ip']}")
    
    while True:
        choice = input("请选择实例编号: ").strip()
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(instances):
                return instances[idx]
            print_error("编号超出范围")
        except ValueError:
            print_error("请输入有效数字")

# 刷AMD CPU循环
def reroll_cpu_loop(project_id, instance_info):
    instance_name = instance_info["name"]
    zone = instance_info["zone"]
    attempt_counter = 0
    max_attempts = 20  # 最大重置次数
    
    instance_client = compute_v1.InstancesClient()
    print_info(f"开始刷AMD CPU，实例: {instance_name} ({zone})")
    
    while attempt_counter < max_attempts:
        print_info(f"\n第 {attempt_counter + 1}/{max_attempts} 次尝试")
        
        # 启动实例（如果未运行）
        try:
            current_inst = instance_client.get(project=project_id, zone=zone, instance=instance_name)
            if current_inst.status != "RUNNING":
                print_info("实例未运行，正在启动...")
                op = instance_client.start(project=project_id, zone=zone, instance=instance_name)
                wait_for_operation(project_id, zone, op.name)
        except Exception as e:
            print_error(f"启动实例失败: {e}")
            attempt_counter += 1
            time.sleep(5)
            continue
        
        # 等待CPU信息同步
        current_platform = None
        for i in range(MAX_RETRIES):
            try:
                current_inst = instance_client.get(project=project_id, zone=zone, instance=instance_name)
                
                if current_inst.status != "RUNNING":
                    print_warning(f"检测到虚拟机状态异常变为: {current_inst.status}。跳过本次检测。")
                    current_platform = "Instability Detected"
                    break
                
                current_platform = current_inst.cpu_platform
                if current_platform and current_platform != "Unknown CPU Platform":
                    break
                
                if (i + 1) % 5 == 0:
                    print_info(f"正在等待 CPU 元数据同步... ({i+1}/{MAX_RETRIES}) - 机器正在启动中")
                time.sleep(2)
            except Exception as e:
                print_error(f"获取实例信息失败: {e}")
                current_platform = "Error"
                break
        
        if current_platform == "Unknown CPU Platform":
            print_warning("超时：等待 2 分钟后仍无法获取 CPU 信息。")
        else:
            print_info(f"检测到 CPU: {current_platform}")
        
        if "AMD" in str(current_platform).upper():
            print_success(f"恭喜！已成功刷到目标 CPU: {current_platform}")
            print_info("脚本执行完毕。")
            break
        
        print_warning(f"结果不满意 ({current_platform})。准备重置...")
        print_info(f"正在关停虚拟机 {instance_name}...")
        try:
            op = instance_client.stop(project=project_id, zone=zone, instance=instance_name)
            wait_for_operation(project_id, zone, op.name)
            # 等待实例完全停止
            time.sleep(5)
            # 启动实例
            print_info(f"正在重启虚拟机 {instance_name}...")
            op = instance_client.start(project=project_id, zone=zone, instance=instance_name)
            wait_for_operation(project_id, zone, op.name)
        except Exception as e:
            print_error(f"重置实例失败: {e}")
        
        attempt_counter += 1
        time.sleep(2)
    else:
        print_warning(f"已达到最大尝试次数 ({max_attempts})，停止刷CPU")

# 读取CDN IP列表
def read_cdn_ips(filename="cdnip.txt"):
    if not os.path.exists(filename):
        print_error(f"找不到文件: {filename}")
        print("请在脚本同目录下创建该文件，并填入IP段。")
        return []

    ip_list = []
    with open(filename, "r", encoding="utf-8") as f:
        for line in f:
            clean_line = line.strip()
            if clean_line and not clean_line.startswith("#"):
                ip = clean_line.split()[0]
                ip_list.append(ip)

    print_info(f"已从 {filename} 读取到 {len(ip_list)} 个 IP 段。")
    return ip_list

# 设置协议字段（兼容不同版本SDK）
def set_protocol_field(config_object, value):
    try:
        config_object.ip_protocol = value
    except AttributeError:
        try:
            config_object.I_p_protocol = value
        except AttributeError:
            print_error(f"\n【调试信息】无法设置协议字段。对象 '{type(config_object).__name__}' 的有效属性如下:")
            print([d for d in dir(config_object) if not d.startswith("_")])
            raise

# 添加允许所有入站规则
def add_allow_all_ingress(project_id, network):
    firewall_client = compute_v1.FirewallsClient()
    rule_name = "allow-all-ingress-custom"

    print_info(f"\n正在创建入站规则: {rule_name} ...")

    firewall_rule = compute_v1.Firewall()
    firewall_rule.name = rule_name
    firewall_rule.direction = "INGRESS"
    firewall_rule.network = network
    firewall_rule.priority = 1000
    firewall_rule.source_ranges = ["0.0.0.0/0"]

    allow_config = compute_v1.Allowed()
    set_protocol_field(allow_config, "all")
    firewall_rule.allowed = [allow_config]

    try:
        operation = firewall_client.insert(project=project_id, firewall_resource=firewall_rule)
        print_info("正在应用规则...")
        operation_client = compute_v1.GlobalOperationsClient()
        operation_client.wait(project=project_id, operation=operation.name)
        print_success("已添加允许所有入站连接的规则。")
    except exceptions.AlreadyExists:
        print_warning(f"规则 {rule_name} 已存在。")
    except Exception as e:
        print_error(f"【失败】{e}")
        traceback.print_exc()

# 添加拒绝CDN出站规则
def add_deny_cdn_egress(project_id, ip_ranges, network):
    if not ip_ranges:
        print_info("IP 列表为空，跳过创建拒绝规则。")
        return

    firewall_client = compute_v1.FirewallsClient()
    rule_name = "deny-cdn-egress-custom"

    print_info(f"\n正在创建出站拒绝规则: {rule_name} ...")

    firewall_rule = compute_v1.Firewall()
    firewall_rule.name = rule_name
    firewall_rule.direction = "EGRESS"
    firewall_rule.network = network
    firewall_rule.priority = 900
    firewall_rule.destination_ranges = ip_ranges

    deny_config = compute_v1.Denied()
    set_protocol_field(deny_config, "all")
    firewall_rule.denied = [deny_config]

    try:
        operation = firewall_client.insert(project=project_id, firewall_resource=firewall_rule)
        print_info("正在应用规则...")
        operation_client = compute_v1.GlobalOperationsClient()
        operation_client.wait(project=project_id, operation=operation.name)
        print_success(f"已添加拒绝规则，共拦截 {len(ip_ranges)} 个 IP 段。")
    except exceptions.AlreadyExists:
        print_warning(f"规则 {rule_name} 已存在。")
    except Exception as e:
        print_error(f"【失败】{e}")
        traceback.print_exc()

# 配置防火墙
def configure_firewall(project_id, network):
    print("\n------------------------------------------------")
    print("防火墙规则管理菜单")
    print("------------------------------------------------")
    print(f"目标网络: {network}")

    choice_in = input("\n[1/2] 是否添加【允许所有入站连接 (0.0.0.0/0)】规则? (y/n): ").strip().lower()
    if choice_in == "y":
        add_allow_all_ingress(project_id, network)
    else:
        print_info("已跳过入站规则配置。")

    choice_out = input("\n[2/2] 是否添加【拒绝对 cdnip.txt 中 IP 的出站连接】规则? (y/n): ").strip().lower()
    if choice_out == "y":
        ips = read_cdn_ips()
        if ips:
            if len(ips) > 256:
                print_warning(f"【警告】IP 数量 ({len(ips)}) 超过 GCP 单条规则上限 (256)。")
                print_info("脚本将只取前 256 个 IP。")
                ips = ips[:256]

            add_deny_cdn_egress(project_id, ips, network)
    else:
        print_info("已跳过出站规则配置。")

    print("\n所有操作完成。")

# 检查是否是未找到错误
def is_not_found_error(exc):
    msg = str(exc).lower()
    return "notfound" in msg or "not found" in msg or "404" in msg or isinstance(exc, exceptions.NotFound)

# 删除防火墙规则
def delete_firewall_rule(project_id, rule_name):
    firewall_client = compute_v1.FirewallsClient()
    try:
        operation = firewall_client.delete(project=project_id, firewall=rule_name)
        operation_client = compute_v1.GlobalOperationsClient()
        operation_client.wait(project=project_id, operation=operation.name)
        print_success(f"已删除防火墙规则: {rule_name}")
        return True
    except Exception as e:
        if is_not_found_error(e):
            print_info(f"防火墙规则不存在，已跳过: {rule_name}")
            return True
        print_warning(f"删除防火墙规则失败: {rule_name} ({e})")
        return False

# 删除磁盘
def delete_disks_if_needed(project_id, zone, disk_names):
    if not disk_names:
        return True
    disk_client = compute_v1.DisksClient()
    all_ok = True
    for disk_name in disk_names:
        try:
            operation = disk_client.delete(project=project_id, zone=zone, disk=disk_name)
            wait_for_operation(project_id, zone, operation.name)
            print_success(f"已删除磁盘: {disk_name}")
        except Exception as e:
            if is_not_found_error(e):
                print_info(f"磁盘不存在，已跳过: {disk_name}")
            else:
                print_warning(f"删除磁盘失败: {disk_name} ({e})")
                all_ok = False
    return all_ok

# 删除免费资源
def delete_free_resources(project_id, instance_info):
    instance_name = instance_info["name"]
    zone = instance_info["zone"]

    print("\n------------------------------------------------")
    print("即将删除以下资源（可以重新创建免费资源）：")
    print(f"- 实例: {instance_name} ({zone})")
    print(f"- 相关磁盘（如仍存在）")
    print(f"- 防火墙规则: {', '.join(FIREWALL_RULES_TO_CLEAN)}")
    confirm = input("请输入 DELETE 确认删除: ").strip()
    if confirm != "DELETE":
        print_info("已取消删除操作。")
        return False

    instance_client = compute_v1.InstancesClient()
    disk_names = []
    try:
        inst = instance_client.get(project=project_id, zone=zone, instance=instance_name)
        for disk in inst.disks:
            if disk.source:
                disk_names.append(disk.source.split("/")[-1])
    except Exception as e:
        print_warning(f"读取实例信息失败，磁盘清理可能不完整: {e}")

    print_info("正在删除实例...")
    try:
        operation = instance_client.delete(project=project_id, zone=zone, instance=instance_name)
        wait_for_operation(project_id, zone, operation.name)
        print_success("实例已删除。")
    except Exception as e:
        if is_not_found_error(e):
            print_info("实例不存在，已跳过删除。")
        else:
            print_warning(f"实例删除失败: {e}")
            return False

    delete_disks_if_needed(project_id, zone, disk_names)

    print_info("正在清理防火墙规则...")
    for rule_name in FIREWALL_RULES_TO_CLEAN:
        delete_firewall_rule(project_id, rule_name)

    print_success("清理完成。建议到控制台确认无残留资源。")
    return True

# 选择远程执行方式
def pick_remote_method():
    has_gcloud = shutil.which("gcloud") is not None
    has_ssh = shutil.which("ssh") is not None

    if not has_gcloud and not has_ssh:
        print_warning("本机未发现 gcloud 或 ssh，无法执行远程脚本。")
        return None

    if has_gcloud:
        choice = input("是否使用 gcloud compute ssh 远程执行? (Y/n): ").strip().lower()
        if choice in ("", "y", "yes"):
            return {"method": "gcloud"}

    if not has_ssh:
        print_warning("未找到 ssh 命令，无法继续。")
        return None

    default_user = getpass.getuser()
    ssh_user = input(f"请输入 SSH 用户名 (默认 {default_user}): ").strip() or default_user
    ssh_port = input("请输入 SSH 端口 (默认 22): ").strip() or "22"
    ssh_key = input("请输入 SSH 私钥路径 (留空表示使用默认密钥): ").strip()
    return {"method": "ssh", "user": ssh_user, "port": ssh_port, "key": ssh_key}

# 构建远程下载脚本命令
def build_remote_download_command(script_url):
    return (
        "set -e;"
        "if command -v curl >/dev/null 2>&1; then DL=\"curl -fsSL\";"
        "elif command -v wget >/dev/null 2>&1; then DL=\"wget -qO-\";"
        "else echo \"error: curl or wget not found\"; exit 1; fi;"
        "tmp=$(mktemp /tmp/gcp_free.XXXXXX.sh);"
        f"$DL \"{script_url}\" > \"$tmp\";"
        "sudo bash \"$tmp\";"
        "rm -f \"$tmp\""
    )

# 构建远程执行命令
def build_remote_exec_command(project_id, instance_info, remote_config, remote_command):
    instance_name = instance_info["name"]
    zone = instance_info["zone"]
    method = remote_config.get("method")

    if method == "gcloud":
        return [
            "gcloud",
            "compute",
            "ssh",
            instance_name,
            "--project",
            project_id,
            "--zone",
            zone,
            "--command",
            remote_command,
        ]
    if method == "ssh":
        host = instance_info.get("external_ip")
        if not host or host == "-":
            print_warning("该实例没有外网 IP，无法使用 SSH 直连。")
            return None
        cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10"]
        port = remote_config.get("port")
        if port:
            cmd += ["-p", str(port)]
        key_path = remote_config.get("key")
        if key_path:
            cmd += ["-i", key_path]
        cmd += [f"{remote_config.get('user')}@{host}", remote_command]
        return cmd

    print_warning("远程执行方式未设置。")
    return None

# 构建远程上传命令
def build_remote_upload_command(project_id, instance_info, remote_config, local_path, remote_path):
    instance_name = instance_info["name"]
    zone = instance_info["zone"]
    method = remote_config.get("method")

    if method == "gcloud":
        return [
            "gcloud",
            "compute",
            "scp",
            local_path,
            f"{instance_name}:{remote_path}",
            "--project",
            project_id,
            "--zone",
            zone,
        ]
    if method == "ssh":
        if shutil.which("scp") is None:
            print_warning("未找到 scp 命令，无法上传文件。")
            return None
        host = instance_info.get("external_ip")
        if not host or host == "-":
            print_warning("该实例没有外网 IP，无法使用 SSH 直连。")
            return None
        cmd = ["scp", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10"]
        port = remote_config.get("port")
        if port:
            cmd += ["-P", str(port)]
        key_path = remote_config.get("key")
        if key_path:
            cmd += ["-i", key_path]
        cmd += [local_path, f"{remote_config.get('user')}@{host}:{remote_path}"]
        return cmd

    print_warning("远程执行方式未设置。")
    return None

# 执行远程脚本
def run_remote_script(project_id, instance_info, script_key, remote_config):
    script_url = REMOTE_SCRIPT_URLS.get(script_key)
    if not script_url:
        print_warning("未知的脚本类型，无法执行。")
        return False
    remote_command = build_remote_download_command(script_url)
    cmd = build_remote_exec_command(project_id, instance_info, remote_config, remote_command)
    if not cmd:
        return False

    print_info(f"正在远程执行脚本: {script_url}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print_success("远程脚本执行完成。")
            return True
        print_warning(f"远程脚本执行失败，退出码: {result.returncode}")
        print_error(f"错误输出: {result.stderr}")
        return False
    except Exception as e:
        print_warning(f"远程执行失败: {e}")
        return False

# 选择流量监控脚本
def select_traffic_monitor_script():
    print("\n--- 请选择流量监控脚本 ---")
    print("[1] 安装 超额关闭 ssh 之外其他入站 (net_iptables.sh)")
    print("[2] 安装 超额自动关机 (net_shutdown.sh)")
    print("[0] 返回")
    while True:
        choice = input("请输入数字选择: ").strip()
        if choice == "1":
            return "net_iptables"
        if choice == "2":
            return "net_shutdown"
        if choice == "0":
            return None
        print_error("输入无效，请重试。")

# 部署dae配置
def deploy_dae_config(project_id, instance_info, remote_config):
    local_config = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.dae")
    if not os.path.isfile(local_config):
        print_warning(f"找不到本地配置文件: {local_config}")
        return False

    remote_tmp = "/tmp/config.dae"
    upload_cmd = build_remote_upload_command(
        project_id,
        instance_info,
        remote_config,
        local_config,
        remote_tmp,
    )
    if not upload_cmd:
        return False

    print_info("正在上传 config.dae ...")
    try:
        result = subprocess.run(upload_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print_warning(f"上传失败，退出码: {result.returncode}")
            print_error(f"错误输出: {result.stderr}")
            return False
    except Exception as e:
        print_warning(f"上传失败: {e}")
        return False

    remote_command = (
        "set -e;"
        "sudo mkdir -p /usr/local/etc/dae;"
        "sudo cp /tmp/config.dae /usr/local/etc/dae/config.dae;"
        "sudo chmod 600 /usr/local/etc/dae/config.dae;"
        "sudo systemctl enable dae || true;"
        "sudo systemctl restart dae || true;"
        "rm -f /tmp/config.dae"
    )
    exec_cmd = build_remote_exec_command(project_id, instance_info, remote_config, remote_command)
    if not exec_cmd:
        return False

    print_info("正在应用配置并重启 dae ...")
    try:
        result = subprocess.run(exec_cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print_success("配置已更新并重启 dae。")
            return True
        print_warning(f"配置应用失败，退出码: {result.returncode}")
        print_error(f"错误输出: {result.stderr}")
        return False
    except Exception as e:
        print_warning(f"配置应用失败: {e}")
        return False

# 主函数
def main():
    print("================================================")
    print("GCP 免费服务器多功能管理工具")
    print("================================================")
    
    # 检查GCP认证
    if not os.path.exists(os.path.expanduser("~/.config/gcloud/application_default_credentials.json")):
        print_warning("未检测到GCP应用默认凭据，尝试使用gcloud认证...")
        try:
            subprocess.run(["gcloud", "auth", "application-default", "login"], check=True)
        except Exception as e:
            print_error(f"GCP认证失败: {e}")
            sys.exit(1)
    
    project_id = select_gcp_project()
    current_instance = None
    remote_config = None

    while True:
        print("\n================================================")
        print(f"当前项目: {project_id}")
        if current_instance:
            print(f"当前服务器: {current_instance['name']} ({current_instance['zone']})")
            print(f"外网IP: {current_instance['external_ip']}")
        else:
            print("当前服务器: 未选择")
        print("------------------------------------------------")
        print("[1] 新建免费实例")
        print("[2] 选择服务器")
        print("[3] 刷 AMD CPU")
        print("[4] 配置防火墙规则")
        print("[5] Debian换源")
        print("[6] 安装 dae")
        print("[7] 上传 config.dae 并启用 dae")
        print("[8] 安装流量监控脚本（仅适配 Debian）")
        print("[9] 删除当前免费资源")
        print("[0] 退出")
        choice = input("请输入数字选择: ").strip()

        if choice == "1":
            zone = select_zone(project_id)
            os_config = select_os_image()
            create_instance(project_id, zone, os_config)
        elif choice == "2":
            current_instance = select_instance(project_id)
        elif choice == "3":
            if not current_instance:
                current_instance = select_instance(project_id)
            if current_instance:
                reroll_cpu_loop(project_id, current_instance)
        elif choice == "4":
            if not current_instance:
                current_instance = select_instance(project_id)
            if current_instance:
                network = current_instance.get("network") or "global/networks/default"
                configure_firewall(project_id, network)
        elif choice == "5":
            if not current_instance:
                current_instance = select_instance(project_id)
            if current_instance:
                if not remote_config:
                    remote_config = pick_remote_method()
                if remote_config:
                    run_remote_script(project_id, current_instance, "apt", remote_config)
        elif choice == "6":
            if not current_instance:
                current_instance = select_instance(project_id)
            if current_instance:
                if not remote_config:
                    remote_config = pick_remote_method()
                if remote_config:
                    run_remote_script(project_id, current_instance, "dae", remote_config)
        elif choice == "7":
            if not current_instance:
                current_instance = select_instance(project_id)
            if current_instance:
                if not remote_config:
                    remote_config = pick_remote_method()
                if remote_config:
                    deploy_dae_config(project_id, current_instance, remote_config)
        elif choice == "8":
            if not current_instance:
                current_instance = select_instance(project_id)
            if current_instance:
                script_key = select_traffic_monitor_script()
                if script_key:
                    if not remote_config:
                        remote_config = pick_remote_method()
                    if remote_config:
                        run_remote_script(project_id, current_instance, script_key, remote_config)
        elif choice == "9":
            if not current_instance:
                current_instance = select_instance(project_id)
            if current_instance:
                if delete_free_resources(project_id, current_instance):
                    current_instance = None
        elif choice == "0":
            print_success("已退出。")
            break
        else:
            print_error("输入无效，请重试。")

if __name__ == "__main__":
    # 设置默认编码
    if sys.platform == "win32":
        os.system("chcp 65001 >nul")
    
    # 检查依赖
    try:
        from google.cloud import compute_v1
    except ImportError:
        print_error("缺少依赖包 google-cloud-compute")
        print_info("正在自动安装...")
        subprocess.run([sys.executable, "-m", "pip", "install", "google-cloud-compute>=1.19.0"], check=True)
        from google.cloud import compute_v1

    try:
        main()
    except KeyboardInterrupt:
        print("\n\033[33m[用户终止] 脚本已停止。\033[0m")
    except Exception as e:
        print(f"\n\033[31m[错误] 发生异常: {e}\033[0m")
        traceback.print_exc()
        sys.exit(1)
