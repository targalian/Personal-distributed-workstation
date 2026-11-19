# 第二节点独立 Python 3.12 安装记录

2026-11-13，用户明确选择执行用户目录独立 Python 3.12 安装，保留系统 Python 3.10，不启停现有服务。

目标：`zero-mass-foam@192.168.100.30`，Linux x86_64，glibc 2.35。

## 安装结果

- 独立解释器：`/home/zero-mass-foam/.local/opt/lan-mesh/python-3.12.14/bin/python3.12`
- 独立虚拟环境：`/home/zero-mass-foam/.local/share/lan-mesh/venvs/py312`
- 两个环境实际版本均为 **Python 3.12.14**，pip **25.0.1**。
- `/usr/bin/python3` 仍为 **3.10.12**；未修改系统包、默认PATH、shell启动文件或既有服务。
- 未安装LAN Mesh项目依赖；两个环境的site-packages均只含pip。

来源为本机已使用的DS Bot主运行时中的独立Python3.12.14。打包排除了site-packages与字节码缓存，不携带Office依赖、业务配置或凭据。pip通过解释器自带ensurepip轮子离线初始化。

传输包：42,286,062 bytes；远端安装前及完成后均核对SHA256：

```text
efae95271d5e9beaa194446f23e29eac62268f0108ffb87760072aa214b0bd82
```

安装目标原先不存在；使用新目录，未覆盖现有解释器或虚拟环境。

## 实际验证

基础解释器和venv各自均完成：

- Python版本、sys.prefix/base_prefix隔离关系；pip可运行且路径属于各自环境。
- SSL、SQLite、ctypes、multiprocessing、venv模块导入。
- SQLite内存数据库执行查询得到42。
- bz2/lzma/zlib压缩和解压往返一致。
- OpenSSL **3.5.8**，SQLite **3.53.1**。
- 默认SSL证书目录存在；显式加载系统CA bundle成功，120个CA。未进行外部TLS联网验证。

最初检查错误地要求默认SSL context立即加载CA；目录按需加载时初始计数为0，导致检查误报，安装本身已完成。随后按目录与CA bundle核验通过；没有修改系统证书或关闭证书校验。

本机完整验证结果：`/tmp/lanmesh-python312-install-pjb86_zs/validation.json`。
远端持久回执：`/home/zero-mass-foam/.local/opt/lan-mesh/python-3.12.14/installation-receipt.json`。

## 使用

```bash
ssh zero-mass-foam@192.168.100.30
source ~/.local/share/lan-mesh/venvs/py312/bin/activate
python --version
python -m pip --version
```

也可直接执行venv中的绝对路径，无需修改默认Python。

Python版本阻塞已解除。项目依赖安装、代码部署、双机协议闭环、升级回滚及在线模型对照尚未在第二节点执行；这次运行时安装不代表双机验收通过。
