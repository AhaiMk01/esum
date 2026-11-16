# 登录、密钥和退出登录

[English](logins.md) · **简体中文**

简要说明在 [README](../README.zh-CN.md#登录与浏览器) 里：ESUM 提示 `LOGIN NEEDED`，登录由你自己完成，ESUM 和 agent 从不接触密码。细节在这一页。

## Discourse 论坛的密钥

Discourse 论坛（USCardForum、LINUX DO）可以保存一个密钥，这样不开浏览器也能以你的账号读取。在你自己的终端运行 `esum uscf login`：它会问权限（`read`，或者要用书签时选 `read,write`），在浏览器里打开授权页，你授权后把页面上的 payload 粘贴回来即可。

- 密钥存在系统凭据库（Windows 凭据管理器、macOS 钥匙串、Linux Secret Service；没有时存在 `~/.esum/auth/`），不会打印出来。
- 要撤销，去论坛的偏好设置 → 应用（`/my/preferences/apps`）。ESUM 不会替你撤销密钥。
- `esum keys` 列出已存的密钥，但不显示密钥本身。
- `esum keys ui` 打开一个本地页面，经 Windows Hello、Touch ID 或指纹确认是你本人后，可以添加、替换或删除 ESUM 保存的那一份。

## 退出某个站点在 ESUM 浏览器里的登录

跟 agent 说一声。它会删掉这个站点在 ESUM 自己的 Chrome 配置里的 cookie，绝不碰你日常用的 Chrome。这需要 patchright-cli 0.13.0 或更新版本，它的 `cookie-clear` 支持 `--domain`（[AhaiMk01/patchright-cli#54](https://github.com/AhaiMk01/patchright-cli/issues/54)）；版本太旧时 agent 会请你升级。

## 为什么不直接复制 Chrome 的 cookie？

在 Windows 上，Chrome 的 App-Bound Encryption 把 cookie 绑定在你真实的配置上，所以 Chrome 会把复制出来的配置或 cookie 文件当成未登录。`native` 模式改为在你同意后，直接问正在运行的 Chrome。
