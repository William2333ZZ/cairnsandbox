# 把自己的 Docker 镜像做成模板

目标:你有一个装好了依赖的 Docker 镜像(比如 `python:3.12-slim` 加你的库),想要的是"开一台沙箱,环境已经在,85 毫秒级热启动"。路线是三步:

1. **镜像 → 层**:`POST /builds` 把镜像的文件系统做成一个只读层。
2. **层 → 准备好的沙箱**:用这个层建沙箱,把环境跑一遍(装包、预热、起服务)。
3. **沙箱 → 命名模板**:把这台沙箱此刻的内存与磁盘提交成模板,之后 `template_key=<名字>` 从它热启动,内存里的进程也还在。

**一步到位**(D69):下面三步 SDK / 命令行可以一次做完——

```bash
cairn build py-data --from python:3.12-slim --run 'pip install --user pandas' --allow pypi.org --allow files.pythonhosted.org
```
```python
c.templates.build("py-data", base="python:3.12-slim", run=["pip install --user pandas"], allow=["pypi.org", "files.pythonhosted.org"])
```
它依次:镜像成层 → 用层起一台沙箱(`allow` 是安装命令能连的域名)→ 写 `files`、逐条执行 `run`(任何一条非 0 就中止并带出输出)→ 提交成模板 → **无论成败都删掉准备用的沙箱**。之后 `template_key="py-data"` 建沙箱。JS SDK 目前没有这个封装,用下面的三步自己组合。需要精细控制(启动常驻服务、预热)时用手动的三步。

概念和界面见[手册第 7 节](../manual.md);字段见 [CONTRACT §1](../../../CONTRACT.md) 的"镜像"条与 [api/images.md](../api/images.md)。

## 第 1 步:镜像成层

节点能连上镜像仓库(默认 docker hub,可由部署者改成镜像站)时,直接给镜像名:

```python
from cairnsandbox import Client
c = Client()                                               # CAIRN_URL / CAIRN_TOKEN / CAIRN_PROJECT
b = c.builds.from_oci("python:3.12-slim", name="py312-base")   # 等价于 POST /builds {kind:"oci", image, name}
b = c.builds.wait(b["id"])                                  # 轮询 GET /builds/<id> 到 done 或 failed
print(b["status"], b.get("result"), b.get("error"))
```

构建是异步的:`POST /builds` 立刻返回 201 和 `{id, project, kind, input, node, status: "running"}`,节点在最空闲的活节点上拉清单与各层、按顺序应用删除标记、写成 ext4 层。完成后 `status` 是 `done`,结果里有**层 id**(形如 `L-xxxxxxxx`;也能在 `c.layers.list()` 或控制台「模板与构建」的"层"表里看到)。失败时 `error` 里是仓库的错误。

在工作台做同样的事:「控制台 → 模板与构建」,构建表头选"OCI 镜像 → 层",填镜像名与层名,点"开始"。

只取 linux/amd64 的清单;私有仓库的凭证还没接,只能拉公开镜像。

### 节点连不上仓库:用归档

在能拉镜像的机器上:

```bash
docker pull myorg/myapp:1.4
docker save myorg/myapp:1.4 -o myapp.tar        # 也可以是 OCI layout 的 tar
```

```python
b = c.builds.from_archive(open("myapp.tar", "rb").read(), name="myapp-1.4")
b = c.builds.wait(b["id"])
```

对应 `POST /builds/archive?name=myapp-1.4`,body 是 tar 的原始字节。控制面只转发不存。两点:

- 请求体上限是 `CP_MAX_BODY_MIB`(默认 512 MiB),更大的归档会得到 413;镜像大就让部署者调大,或先瘦身。
- 指定落在哪台节点的 `node=` 参数归部署者用;成员不带,由控制面挑最空闲的活节点。

## 第 2 步:用层建沙箱,把环境跑一遍

```python
layer = b["result"]["id"]                    # 控制台的"结果"列显示的就是它
sb = c.sandboxes.create(layers=[layer], mem=1024, cls="persistent",
                        network={"allow_hosts": ["pypi.org", "files.pythonhosted.org"]})
sb.commands.run("python3 -c 'import sys; print(sys.version)'").check()
sb.commands.run("pip install --user numpy pandas", timeout=900).check()
sb.commands.run("python3 -c 'import numpy, pandas'").check()      # 预热:把 import 的缓存也带进内存
```

`layers=[...]` 叠在我们的基础镜像之上。"准备好"的意思由你定:装依赖、下载模型文件、启动一个常驻服务、让解释器把常用库 import 一遍。提交的是**内存加磁盘**,所以内存里已经起好的进程,新沙箱一出生就在。

如果还没装 `pip` 能用的包,而镜像里的 Python 不在默认路径上,先 `sb.commands.run("which python3 pip")` 看清楚(见下面"注意事项"里的"基础赢")。

## 第 3 步:提交成命名模板

```python
b2 = sb.commit("py312-ready", note="numpy pandas preloaded")     # POST /builds {kind:"commit", sandbox, name, note}
b2 = c.builds.wait(b2["id"])
print(b2["status"])
sb.kill()                                                       # 提交完这台就可以丢了
print([t["key"] for t in c.templates.list()])                    # 模板的名字出现在列表里
```

控制台里做法:构建表头选"沙箱 → 模板",填沙箱 id 与模板名,点"开始"。提交时沙箱短暂停顿,之后继续跑。

## 之后:热启动

```python
sb2 = c.sandboxes.create(template_key="py312-ready", layers=[layer], mem=1024, cls="persistent",
                         network={"allow_hosts": ["pypi.org"]})
print(sb2.info["boot_mode"])                  # 命中模板快照时是 snapshot,不是 cold
print(sb2.commands.run("python3 -c 'import numpy; print(numpy.__version__)'").stdout)
```

**配置须与模板一致**:`template`(基础系统)、`vcpu`、`mem`、`layers` 要和提交那台沙箱相同,不一致创建会返回 400。"模板"表的"配置"列给出模板的系统与规格。为省事,把这组参数存成一个常量,建原始沙箱与热启动都用它。

模板与层住在**提交它们的那台节点**上。多节点部署里,让部署者用 `POST /templates/everywhere`、`POST /layers/<id>/everywhere` 分发,控制台"分发矩阵"里变成实心才算到位;没分发时,别的节点上没有这个命名模板。

要通过 E2B 形状的口用它:E2B 的 `templateID` 就填模板名,见[从 E2B 迁移](migrate-from-e2b.md)。

## 注意事项

- **基础赢**。我们的沙箱不是容器:guest 的 init 与 `sandboxd`、`/bin`、libc、`/etc/passwd` 等来自我们的基础镜像。构建层时,镜像里与基础镜像**重名的文件一律剔掉**,镜像只补基础没有的。后果:镜像自带的更新版 libc 或别的 shell 不会生效,`agent` 用户和基础的系统文件保持我们的;你的应用目录、`/opt`、`/usr/local` 下基础没有的东西都会在。装好后跑一遍你的程序确认。
- **`ENTRYPOINT`、`CMD`、`ENV` 不执行也不解释**。沙箱里的进程由你(或 harness)起。镜像里 `ENV` 设的变量要自己用创建时的 `env={...}` 补,启动命令放进第 2 步里自己跑。
- **没有 Dockerfile 构建**。我们接受已经建好的镜像(仓库拉取或归档),不读 Dockerfile。
- **私有仓库的凭证**还没接;节点连不上仓库的、私有的镜像都走归档。
- **多架构**只取 amd64。
- **构建没有日志流**,只有 `status`、`result`、`error`。
- **提交的是此刻的内存**:里面带着的 token、密钥、会话也会进模板。别在提交前把密钥读进环境;出口凭证的注入走保险库([手册第 5 节](../manual.md)),密钥本来就不进沙箱。
- 模板与层归节点共享,成员可用,构建归自己的项目;合同里没有删除模板与层的接口,命名要有章法(带版本号)。
