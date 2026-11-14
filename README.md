# 网盘直链频道（MoviePilot V3 插件）

把**夸克分享链接**转成 MoviePilot 眼里的正常种子资源，由插件完成「取链 → 分片下载」，
其余流程（订阅、过滤、下载管理、整理、刮削、STRM、Emby、通知）**全部复用 MoviePilot 原生实现**。

- 不修改 MoviePilot 内核
- 不新增 `DownloaderType` / `StorageSchema`
- 不依赖 CloudSubscribeX

## 它是怎么骗过 MoviePilot 的

1. `search_torrents(site={})`：插件作为「插件资源源」参与每次搜索，返回 `TorrentInfo`。
2. `enclosure` 指向本插件的 `/api/v1/plugin/QuarkChannel/torrent?sid=…&sig=…`，
   该接口把分享文件清单编码成**合法 bencode 种子**（文件名/大小/集数都是真的）。
3. MoviePilot 正常解析种子、按集数选集，调用插件的 `download()`。
4. 插件按 `episodes` 过滤文件 → 取夸克直链 → 8–64MB 分片并发下载到本地目录。
5. `list_torrents()` 回报进度 → MoviePilot 完成下载判定 → 自动整理。

## 目录

```
plugins.v3/quarkchannel/     # 插件源码
package.v3.json              # 市场索引
icons/QuarkChannel.png
tests/v3/quarkchannel/       # 纯逻辑单测（离线可跑）
docs/plan/                   # 调研与架构文档
```

## 部署（本地仓库模式）

1. 把本仓库放到 MoviePilot 可访问的目录，例如容器内 `/config/plugin_repos/moviepilot-quarkchannel`。
2. 给 MoviePilot 配置环境变量：
   ```
   PLUGIN_LOCAL_REPO_PATHS=/config/plugin_repos
   ```
3. 重启 MoviePilot → 插件市场「本地」分类出现「网盘直链频道」→ 安装 → 启用。
4. 配置项：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| 启用插件 | 关 | 关闭时不参与搜索，MoviePilot 回到原生 BT/PT |
| 盘搜服务地址 | 空 | 自建 pansou，例如 `http://pansou:8888` |
| 盘搜账号 / 密码 | 空 | pansou 开启 `AUTH_ENABLED` 时填写，插件自动登录换 JWT |
| 盘搜访问令牌 | 空 | 也可直接填现成 JWT，优先于账号密码 |
| MoviePilot 内部地址 | `http://127.0.0.1:3000` | 插件生成种子 URL 用，MoviePilot 自己会来拉 |
| 夸克 Cookie | 空 | 可选，填了降低匿名请求被限概率 |
| 下载根目录 | `/downloads` | 必须与 MoviePilot 下载目录一致 |
| 并发下载任务数 | 3 | 同时下载的分享数 |
| 仅白名单关键词生效 | 关 | 开后可限定只在指定关键词上出场 |
| 搜索结果按集展开 | 关 | 关闭时一个分享只占一条结果，可在「搜索下载」页选集；开启后兼容旧的逐集平铺 |

## 手工验证链路

```bash
# 1) 登记一条分享（返回合成种子地址）
curl -X POST "$MP/api/v1/plugin/QuarkChannel/share" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"share_url":"https://pan.quark.cn/s/xxxx","passcode":""}'

# 2) 拉种子，确认能被解析
curl -s "$MP/api/v1/plugin/QuarkChannel/torrent?sid=…&sig=…" -o /tmp/t.torrent
python3 -c "from torrentool.api import Torrent; t=Torrent.from_file('/tmp/t.torrent'); print(t.name, len(t.files))"

# 3) 查看任务
curl "$MP/api/v1/plugin/QuarkChannel/status" -H "Authorization: Bearer $TOKEN"
```

## 排障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 搜索页没有本插件资源 | 未启用 / 未配 pansou / 关键词被白名单挡掉 | 看插件日志 `quarkchannel` 关键字 |
| 点下载提示「无法获取下载地址」 | 种子接口 404（分享失效）或签名不匹配 | 调用 `/share` 重新登记，检查 `sign_secret` 是否被改 |
| 下载进度不动 | 直链被限速/失效 | 停掉任务再继续（会重新取链），或换候选资源 |
| 传错目录 | `download_root` 与 MoviePilot 下载目录不一致 | 两边配成同一路径 |
| 整理不动 | 任务状态不是 completed | `list_torrents` 只在文件校验通过后置 completed |

## 文档

- [调研：现成方案对比](docs/plan/2026-11-13-MP夸克直链-调研.md)
- [架构：挂点与数据流](docs/plan/2026-11-13-MP夸克直链-架构.md)

## 测试

```bash
python3 -m pytest tests/v3/quarkchannel -q
```
