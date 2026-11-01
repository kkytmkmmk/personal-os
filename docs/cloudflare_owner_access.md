# Cloudflareから本人だけが使う

Python・SQLite・Ollamaは本人のPCに残し、Cloudflare TunnelとAccessで接続します。PCとアプリとTunnelの稼働中だけ利用できます。これは常時稼働クラウドへの移転ではありません。

## 公開する前の必須条件

1. 本人が指定したホスト名を使います。未指定の名前を推測してDNS登録しません。
2. 先にAccessのSelf-hosted Applicationをそのホスト名の全パスへ設定します。既存のGoogle本人認証と本人メールだけのAllowポリシーを利用し、Everyone・Bypass・OTPを追加しません。ほかのアプリやTunnelは変更しません。
3. AccessのTeam URL、Application Audience、本人メールをローカル環境変数またはGit管理外の`.env`へ設定します。実値は公開ドキュメントへ記録しません。

```text
PERSONAL_OS_PUBLIC_ORIGIN=https://os.example.com
PERSONAL_OS_ACCESS_ISSUER=https://your-team.cloudflareaccess.com
PERSONAL_OS_ACCESS_AUDIENCE=your-application-audience
PERSONAL_OS_ACCESS_OWNER_EMAIL=<owner-email>
```

```powershell
python -m pip install -r requirements-cloud.txt
.\tools\start_production.ps1
```

公開HostへのAPI要求ではAccessの署名・Issuer・Audience・有効期限・本人メールを検証します。Tunnel経由では接続元がloopbackでも認証を省略しません。書込みは同一Originのみ許可します。設定不足、依存不足、署名検証不能では拒否します。通常のlocalhost利用は従来どおりです。

## Tunnel設定の確認

```powershell
python tools/plan_cloudflare_tunnel.py --hostname os.example.com --issuer https://your-team.cloudflareaccess.com --audience your-application-audience
```

このコマンドは設定案を表示するだけで、外部リソースを作成しません。API管理Tunnelの設定には、固定のローカル接続先、公開Hostの上書き、Accessトークン必須・Audience指定、最終404を含めます。Access設定を確認してから専用TunnelとDNSを作成します。既存Tunnelを流用・上書きしません。

Cloudflare APIにはTunnel編集、対象ZoneのDNS編集、Accessアプリ編集、既存ポリシー・IdP・Team情報の参照権限が必要です。WranglerにログインできるだけではDNS・Accessの権限があるとは限りません。403の場合は公開を中止し、必要な権限の認証をローカルで設定します。API Tokenをチャットへ貼り付けないでください。

Tunnel Tokenは秘密です。コード・DB・Git・コマンド引数・ログへ保存しません。実行時の`TUNNEL_TOKEN`環境変数、またはリポジトリ外の本人だけが読めるToken Fileを使います。Quick Tunnelによる保護なしの公開は行いません。

## 公開完了の受入確認

- 未認証ではログインへ誘導され、APIデータや添付を読めない。
- 本人以外は拒否される。本人のGoogleログイン後だけデータを使える。
- Hostをlocalhostへ変更した転送要求や偽JWTも拒否される。
- 本人のiPhoneから閲覧・記録・相談ができる。
- PCやTunnelを停止すると公開先へ接続できなくなる。
- APIキーや本人データをPublic Mirrorへ含めていない。

署名・メール・Origin検証のUnit Testは合成鍵と合成ユーザーで実行します。これらのテスト成功だけを外部公開完了とは扱いません。ホスト名、Cloudflare権限、実際の本人ログインの確認が揃わなければ公開は未完了です。
