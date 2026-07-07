# py-frp

一个小型 Python 生态内网穿透工具，设计上参考 frp/rathole 的“服务端公网监听 + 客户端反向拨入”模型。当前版本专注 TCP 端口映射，支持服务端、客户端、frp 常见 TOML/INI 配置子集，以及 rathole 常见 TOML 配置子集。

## 安装

从 PyPI 安装：

```bash
pip install py-simple-nat-tunnel
```

从源码安装：

```bash
pip install .
```

开发模式安装：

```bash
pip install -e .
```

安装后会得到这些命令：

```bash
py-frp --version
py-frp server -c examples/frps.toml
py-frp client -c examples/frpc.toml
py-frps -c examples/frps.toml
py-frpc -c examples/frpc.toml
```

Windows 上如果提示 scripts 目录不在 `PATH`，也可以直接用：

```bash
python -m py_frp server -c examples/frps.toml
python -m py_frp client -c examples/frpc.toml
```

## 快速开始

服务端公网机器：

```bash
py-frp server -c examples/frps.toml
```

客户端内网机器：

```bash
py-frp client -c examples/frpc.toml
```

`examples/frpc.toml` 默认把客户端的 `127.0.0.1:22` 暴露到服务端的 `0.0.0.0:6000`。启动后从外部访问：

```bash
ssh -p 6000 user@your-server
```

## 免配置端口池模式

服务端不需要配置文件，只指定控制端口和公网端口池：

```bash
py-frp server --bind-port 7000 --port-pool 6000-6009
```

`--port-pool` 可以出现多次，服务端会把所有区间和单点端口取并集；`a-b` 表示闭区间，单独的 `a` 表示加入一个端口：

```bash
py-frp server --bind-port 7000 --port-pool 6000-6009 --port-pool 7000 --port-pool 7100-7102
```

启动后服务端会在 stdout 输出一组随机 token。token 只使用容易分辨的字母数字，并排除 `I`、`O`、`0`、`1`、`l`。管理员把 token 记录下来发给客户端。

客户端只需要目标服务端和 token 即可连接；本地目标默认是 `127.0.0.1:22`：

```bash
py-frp client --server your-server:7000 --token TOKEN_FROM_SERVER
```

客户端注册成功后会把服务端分配到的远程端口输出到 stdout，例如：

```text
6003
```

如果本地目标不是 SSH，可以显式指定：

```bash
py-frp client --server your-server:7000 --token TOKEN_FROM_SERVER --local 127.0.0.1:8080
```

同一个 token 同时只允许一个在线客户端。新客户端使用同一个 token 登录时，服务端会关闭旧客户端并释放旧的公网端口，再从端口池中分配一个可用端口给新客户端。

## frp 配置子集

服务端 `frps.toml`：

```toml
bindPort = 7000

[auth]
token = "change-me"
```

客户端 `frpc.toml`：

```toml
serverAddr = "your-server"
serverPort = 7000

[auth]
token = "change-me"

[[proxies]]
name = "ssh"
type = "tcp"
localIP = "127.0.0.1"
localPort = 22
remotePort = 6000
```

也支持旧版 frp INI 的 `[common]` 和 TCP proxy section：

```ini
[common]
server_addr = your-server
server_port = 7000
token = change-me

[ssh]
type = tcp
local_ip = 127.0.0.1
local_port = 22
remote_port = 6000
```

## rathole 配置子集

服务端：

```toml
[server]
bind_addr = "0.0.0.0:2333"
default_token = "change-me"

[server.services.ssh]
bind_addr = "0.0.0.0:6000"
```

客户端：

```toml
[client]
remote_addr = "your-server:2333"
default_token = "change-me"

[client.services.ssh]
local_addr = "127.0.0.1:22"
```

## 提权

服务端启动时如果配置里有小于 1024 的监听端口，默认会通过 `py-admin-launch` 尝试提权一次。也可以显式控制：

```bash
py-frp server -c frps.toml --elevate
py-frp server -c frps.toml --no-auto-elevate
```

实现上会在重启命令后追加内部标记，保证最多只尝试一次，不会循环弹出提权请求。

## 当前范围

已支持：

- TCP 反向端口映射
- 多服务/多 proxy
- token 鉴权
- 免配置 token 端口池模式
- frp TOML 和 legacy INI 的常见 TCP 配置
- rathole TOML 的常见 TCP 配置
- `pip install .` 一键安装和 console scripts

暂不支持：

- UDP
- HTTP/HTTPS vhost
- frp 插件、负载均衡、STCP/XTCP
- rathole Noise/TLS 加密传输
- 多路复用；当前每个公网连接会建立一条独立 tunnel TCP

## 测试

```bash
python -m unittest discover -s tests -v
```
