# v7-r3 文档刷新前候选

该目录保留 v7-r3 文档刷新前的 manifest，仅用于追溯，不用于最终安装或 tag。

```text
archive: helmet-native-app-20261124-v7-r3.pre-doc-refresh.zip
sha256: c428e6002954def2dc7385ffc8d0991369515c6148f9e3fc04838e2b78578283
bytes: 39257593
entries: 45
```

该包与最终包的五文件 `code_identity` 和告警功能身份相同，差别在文档载荷。
板端保留 root：

```text
/userdata/helmet-validation/20261124-native-app-v7-r3-pre-doc-refresh
```

第二次文档核验发现，包内部分相对链接在解压后的目录层级中失效。随后只修改文档
链接并重新构建，得到以下历史包；它同样不用于最终安装或 tag：

```text
archive: helmet-native-app-20261124-v7-r3.pre-link-fix.zip
sha256: c08a82b001cac1620712ba756c6779d96dd5e01114c39752b05792f48f8e4c39
bytes: 39257950
entries: 45
```

该包与最终包的五文件 `code_identity`、告警功能身份和全部运行代码相同，差别只在
文档载荷。最终包 SHA256 为
`553667bec091d565b46557f533b546145d2e0e5ae47af0c03506c140ce8d9df8`。
板端将该历史包对应的运行根保留为：

```text
/userdata/helmet-validation/20261124-native-app-v7-r3-pre-link-fix
```

最终包和 manifest 位于上一级目录。
