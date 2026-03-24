import os
import sys
from google.cloud import compute_v1

# ===================== 核心配置（可根据需求调整） =====================
# 默认区域（可改为 asia-east1/asia-southeast1 等）
DEFAULT_REGION = "us-central1"
# 默认可用区
DEFAULT_ZONE = "us-central1-a"
# 免费实例配置（符合GCP免费额度）
FREE_TIER_MACHINE_TYPE = "e2-micro"
# 默认磁盘大小（GB，免费额度内建议≤30）
DEFAULT_DISK_SIZE_GB = 20

# ===================== 动态获取镜像家族 =====================
def list_image_families(project, filter_str="status=READY AND NOT deprecated.state=DEPRECATED"):
    """通用函数：获取指定项目下的可用镜像家族"""
    try:
        images_client = compute_v1.ImagesClient()
        response = images_client.list(project=project, filter=filter_str)
        families = set()
        for image in response:
            if image.family:
                families.add(image.family)
        return sorted(families)
    except Exception as e:
        print(f"[错误] 获取 {project} 镜像列表失败：{str(e)}")
        return []

def build_os_image_options():
    """构建操作系统选项列表（自动整合 Debian + Ubuntu）"""
    os_options = []
    
    # 1. 处理 Debian 镜像（debian-cloud 项目）
    debian_project = "debian-cloud"
    debian_families = list_image_families(debian_project)
    debian_name_map = {
        "debian-12": "Debian 12 (Bookworm)",
        "debian-13": "Debian 13 (Trixie)",
    }
    for family in debian_families:
        if family in debian_name_map:
            os_options.append({
                "name": debian_name_map[family],
                "project": debian_project,
                "family": family
            })
    
    # 2. 处理 Ubuntu 镜像（ubuntu-os-cloud 项目）
    ubuntu_project = "ubuntu-os-cloud"
    ubuntu_families = list_image_families(ubuntu_project)
    ubuntu_name_map = {}
    for family in ubuntu_families:
        if "ubuntu-2204-lts" == family:
            ubuntu_name_map[family] = "Ubuntu 22.04 LTS"
        elif "ubuntu-2404-lts" == family:
            ubuntu_name_map[family] = "Ubuntu 24.04 LTS"
        elif "ubuntu-2504" == family:
            ubuntu_name_map[family] = "Ubuntu 25.04 (Noble Numbat)"
        elif family.startswith("ubuntu-") and len(family) == 10:  # 匹配 ubuntu-xxxx 格式
            ver = family.replace("ubuntu-", "")
            if len(ver) == 4:
                ver_formatted = f"{ver[:2]}.{ver[2:]}"
                ubuntu_name_map[family] = f"Ubuntu {ver_formatted}"
    
    for family in ubuntu_families:
        if family in ubuntu_name_map:
            os_options.append({
                "name": ubuntu_name_map[family],
                "project": ubuntu_project,
                "family": family
            })
    
    return os_options

# ===================== 用户交互逻辑 =====================
def select_os_image():
    """动态显示操作系统选项并返回用户选择"""
    os_options = build_os_image_options()
    if not os_options:
        print("[错误] 未获取到任何可用的操作系统镜像，请检查GCP认证或网络！")
        sys.exit(1)
    
    print("\n--- 请选择操作系统 ---")
    for idx, option in enumerate(os_options, 1):
        print(f"[{idx}] {option['name']}")
    
    # 输入校验
    while True:
        try:
            choice = int(input(f"\n请输入数字选择 (1-{len(os_options)}): "))
            if 1 <= choice <= len(os_options):
                selected = os_options[choice - 1]
                print(f"[信息] 已选择系统: {selected['name']}")
                return selected
            else:
                print(f"请输入 1-{len(os_options)} 之间的有效数字！")
        except ValueError:
            print("请输入有效的数字（如 1、2、3）！")

def get_user_input(prompt, default=None):
    """通用输入函数，支持默认值"""
    user_input = input(f"{prompt}（默认：{default}）: ").strip()
    return user_input if user_input else default

# ===================== 创建GCP实例核心逻辑 =====================
def create_free_tier_instance(project_id, instance_name, zone, os_image):
    """创建符合GCP免费额度的虚拟机实例"""
    # 初始化客户端
    instances_client = compute_v1.InstancesClient()
    images_client = compute_v1.ImagesClient()
    networks_client = compute_v1.NetworksClient()

    # 1. 获取镜像信息
    try:
        image = images_client.get_from_family(
            project=os_image["project"],
            family=os_image["family"]
        )
        source_disk_image = image.self_link
    except Exception as e:
        print(f"[错误] 获取镜像失败：{str(e)}")
        return False

    # 2. 配置网络（允许SSH和HTTP）
    network_interface = compute_v1.NetworkInterface()
    # 使用默认网络
    network_interface.name = "global/networks/default"
    # 配置外部IP
    access_config = compute_v1.AccessConfig()
    access_config.name = "External NAT"
    access_config.type_ = "ONE_TO_ONE_NAT"
    access_config.network_tier = "PREMIUM"
    network_interface.access_configs = [access_config]

    # 3. 配置磁盘
    disk = compute_v1.AttachedDisk()
    initialize_params = compute_v1.AttachedDiskInitializeParams()
    initialize_params.source_image = source_disk_image
    initialize_params.disk_size_gb = DEFAULT_DISK_SIZE_GB
    initialize_params.disk_type = f"zones/{zone}/diskTypes/pd-standard"
    disk.initialize_params = initialize_params
    disk.auto_delete = True
    disk.boot = True

    # 4. 配置实例
    instance = compute_v1.Instance()
    instance.name = instance_name
    instance.machine_type = f"zones/{zone}/machineTypes/{FREE_TIER_MACHINE_TYPE}"
    instance.disks = [disk]
    instance.network_interfaces = [network_interface]

    # 5. 配置防火墙（允许SSH和HTTP）
    instance.metadata = compute_v1.Metadata()
    instance.metadata.items = [
        compute_v1.Items(key="enable-oslogin", value="TRUE")
    ]

    # 6. 发送创建请求
    request = compute_v1.InsertInstanceRequest()
    request.project = project_id
    request.zone = zone
    request.instance_resource = instance

    try:
        print(f"[信息] 开始创建实例 {instance_name}（区域：{zone}）...")
        operation = instances_client.insert(request=request)
        # 等待创建完成
        operation.result(timeout=300)
        print(f"[成功] 实例 {instance_name} 创建完成！")
        # 获取实例信息（外部IP）
        created_instance = instances_client.get(
            project=project_id,
            zone=zone,
            instance=instance_name
        )
        for interface in created_instance.network_interfaces:
            for config in interface.access_configs:
                print(f"[信息] 实例外部IP：{config.nat_ip}")
        return True
    except Exception as e:
        print(f"[错误] 创建实例失败：{str(e)}")
        return False

# ===================== 主程序入口 =====================
def main():
    print("===== GCP 免费实例创建工具 =====")
    
    # 1. 检查GCP认证
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        print("[警告] 未检测到GCP认证凭证！")
        cred_path = get_user_input("请输入GCP服务账号密钥文件路径", "")
        if cred_path and os.path.exists(cred_path):
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = cred_path
        else:
            print("[错误] 无效的密钥文件路径，程序退出！")
            sys.exit(1)
    
    # 2. 获取项目ID
    project_id = get_user_input("请输入GCP项目ID", "")
    if not project_id:
        print("[错误] 项目ID不能为空！")
        sys.exit(1)
    
    # 3. 选择区域/可用区
    region = get_user_input("请输入实例区域", DEFAULT_REGION)
    zone = get_user_input("请输入实例可用区", DEFAULT_ZONE)
    
    # 4. 输入实例名称
    instance_name = get_user_input("请输入实例名称", f"free-tier-instance-{int(os.time())}")
    
    # 5. 选择操作系统
    selected_os = select_os_image()
    
    # 6. 确认创建
    confirm = input(f"\n是否确认创建以下配置的实例？\n"
                   f"项目ID：{project_id}\n"
                   f"区域/可用区：{region}/{zone}\n"
                   f"实例名称：{instance_name}\n"
                   f"操作系统：{selected_os['name']}\n"
                   f"机器类型：{FREE_TIER_MACHINE_TYPE}\n"
                   f"磁盘大小：{DEFAULT_DISK_SIZE_GB}GB\n"
                   f"请输入 y/N 确认：")
    if confirm.lower() != "y":
        print("[信息] 用户取消创建，程序退出！")
        sys.exit(0)
    
    # 7. 创建实例
    create_free_tier_instance(
        project_id=project_id,
        instance_name=instance_name,
        zone=zone,
        os_image=selected_os
    )

if __name__ == "__main__":
    main()
