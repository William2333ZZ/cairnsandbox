# Turn Your Own Docker Image into a Template

Goal: you have a Docker image with dependencies already installed (say `python:3.12-slim` plus your libraries), and what you want is "open a sandbox with the environment already there, with an 85-millisecond-class hot start". The route has three steps:

1. **Image → layer**: `POST /builds` turns the image's filesystem into a read-only layer.
2. **Layer → prepared sandbox**: create a sandbox from this layer and run the environment through once (install packages, warm up, start services).
3. **Sandbox → named template**: commit this sandbox's current memory and disk as a template; afterwards `template_key=<name>` hot-starts from it, and the processes in memory are still there.

**All in one go** (D69): the three steps below can be done in one shot with the SDK / command line:

```bash
cairn build py-data --from python:3.12-slim --run 'pip install --user pandas' --allow pypi.org --allow files.pythonhosted.org
```
```python
c.templates.build("py-data", base="python:3.12-slim", run=["pip install --user pandas"], allow=["pypi.org", "files.pythonhosted.org"])
```
It does, in order: image to layer → start a sandbox from the layer (`allow` is the domains the install commands may reach) → write `files`, run each `run` entry (any non-zero exit aborts and brings out the output) → commit as a template → **delete the preparation sandbox whether it succeeded or failed**. Afterwards create sandboxes with `template_key="py-data"`. The JS SDK does not have this wrapper yet; combine the three steps below yourself. When you need fine control (starting a resident service, warming up), use the manual three steps.

For concepts and the UI see [manual section 7](../manual.md); for fields see the "Image" entry in [CONTRACT §1](../../../../CONTRACT.md) and [api/images.md](../api/images.md).

## Step 1: image to layer

When the node can reach the image registry (docker hub by default, which the deployer can change to a mirror), just give the image name:

```python
from cairnsandbox import Client
c = Client()                                               # CAIRN_URL / CAIRN_TOKEN / CAIRN_PROJECT
b = c.builds.from_oci("python:3.12-slim", name="py312-base")   # equivalent to POST /builds {kind:"oci", image, name}
b = c.builds.wait(b["id"])                                  # polls GET /builds/<id> until done or failed
print(b["status"], b.get("result"), b.get("error"))
```

The build is asynchronous: `POST /builds` returns 201 immediately with `{id, project, kind, input, node, status: "running"}`; on the most idle live node, the node pulls the manifest and the layers, applies the deletion markers in order, and writes an ext4 layer. When finished, `status` is `done`, and the result contains the **layer id** (of the form `L-xxxxxxxx`; you can also see it in `c.layers.list()` or in the "Layers" table under the console's "Templates & Builds"). On failure, `error` holds the registry's error.

To do the same in the workbench: "Console → Templates & Builds", choose "OCI image → layer" in the build table header, fill in the image name and layer name, and click "Start".

Only the linux/amd64 manifest is taken; credentials for private registries are not wired up yet, so only public images can be pulled.

### Node can't reach the registry: use an archive

On a machine that can pull images:

```bash
docker pull myorg/myapp:1.4
docker save myorg/myapp:1.4 -o myapp.tar        # can also be a tar in OCI layout
```

```python
b = c.builds.from_archive(open("myapp.tar", "rb").read(), name="myapp-1.4")
b = c.builds.wait(b["id"])
```

This corresponds to `POST /builds/archive?name=myapp-1.4`, with the raw bytes of the tar as the body. The control plane only forwards it and does not store it. Two points:

- The request body limit is `CP_MAX_BODY_MIB` (default 512 MiB); a larger archive gets 413. If the image is large, have the deployer raise it, or slim the image down first.
- The `node=` parameter that chooses which node it lands on is for the deployer; members leave it out and the control plane picks the most idle live node.

## Step 2: create a sandbox from the layer and run the environment through

```python
layer = b["result"]["id"]                    # this is what the console's "Result" column shows
sb = c.sandboxes.create(layers=[layer], mem=1024, cls="persistent",
                        network={"allow_hosts": ["pypi.org", "files.pythonhosted.org"]})
sb.commands.run("python3 -c 'import sys; print(sys.version)'").check()
sb.commands.run("pip install --user numpy pandas", timeout=900).check()
sb.commands.run("python3 -c 'import numpy, pandas'").check()      # warm-up: brings the import cache into memory too
```

`layers=[...]` is stacked on top of our base image. What "prepared" means is up to you: install dependencies, download model files, start a resident service, have the interpreter import the commonly used libraries. What gets committed is **memory plus disk**, so processes already started in memory are there the moment a new sandbox is born.

If you haven't installed a package that `pip` can use and the Python in the image is not on the default path, first look with `sb.commands.run("which python3 pip")` (see "Base wins" under "Notes" below).

## Step 3: commit as a named template

```python
b2 = sb.commit("py312-ready", note="numpy pandas preloaded")     # POST /builds {kind:"commit", sandbox, name, note}
b2 = c.builds.wait(b2["id"])
print(b2["status"])
sb.kill()                                                       # once committed, this sandbox can be discarded
print([t["key"] for t in c.templates.list()])                    # the template's name appears in the list
```

In the console: choose "Sandbox → template" in the build table header, fill in the sandbox id and the template name, and click "Start". The sandbox pauses briefly during the commit and then keeps running.

## Afterwards: hot start

```python
sb2 = c.sandboxes.create(template_key="py312-ready", layers=[layer], mem=1024, cls="persistent",
                         network={"allow_hosts": ["pypi.org"]})
print(sb2.info["boot_mode"])                  # snapshot when the template snapshot is hit, not cold
print(sb2.commands.run("python3 -c 'import numpy; print(numpy.__version__)'").stdout)
```

**The configuration must match the template**: `template` (the base system), `vcpu`, `mem`, and `layers` must be the same as on the sandbox that was committed; if they differ, creation returns 400. The "Config" column of the "Templates" table gives the template's system and specs. To make it easy, store this set of parameters in a constant and use it both when creating the original sandbox and when hot-starting.

Templates and layers live on **the node that committed them**. In a multi-node deployment, have the deployer distribute them with `POST /templates/everywhere` and `POST /layers/<id>/everywhere`; it counts as done only when the cell turns solid in the console's "Distribution matrix"; until distributed, the named template does not exist on other nodes.

To use it through the E2B-shaped port: put the template name in E2B's `templateID`; see [Migrate from E2B](migrate-from-e2b.md).

## Notes

- **Base wins**. Our sandbox is not a container: the guest's init and `sandboxd`, `/bin`, libc, `/etc/passwd`, and so on come from our base image. When building a layer, **any file in the image with the same name as one in the base image is dropped**, and the image only supplies what the base lacks. Consequences: an updated libc or another shell that ships with the image does not take effect, and the `agent` user and the base system files stay ours; your application directory, `/opt`, and whatever under `/usr/local` the base lacks will be there. After installing, run your program once to confirm.
- **`ENTRYPOINT`, `CMD`, and `ENV` are neither executed nor interpreted**. Processes in the sandbox are started by you (or a harness). Variables set by `ENV` in the image must be supplied yourself with `env={...}` at creation, and the startup command goes into step 2, run by you.
- **No Dockerfile builds**. We accept an already-built image (registry pull or archive) and do not read Dockerfiles.
- **Credentials for private registries** are not wired up yet; images that the node can't reach or that are private all go through an archive.
- **Multi-arch**: only amd64 is taken.
- **Builds have no log stream**, only `status`, `result`, and `error`.
- **What is committed is the memory as of this moment**: tokens, keys, and sessions held in it also go into the template. Don't read keys into the environment before committing; egress credential injection goes through the vault ([manual section 5](../manual.md)), where keys never enter the sandbox in the first place.
- Templates and layers are shared per node and usable by members, while builds belong to your own project; the contract has no interface for deleting templates and layers, so name them methodically (with a version number).
