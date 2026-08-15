# RoomTrace 実用版開発計画

## 目的

We2 Plus でホテル客室を歩いて撮影し、Windows PC で処理した結果を Blender に読み込んで、実物の部屋を組み立てるための参照モデルを作る。

主成果物は、**実寸スケールのテクスチャ付き GLB**。点群、カメラ、品質レポートは再現性とモデリングを支える補助成果物とする。3DGS は今回の必須経路に含めない。

## 固定する前提

- 撮影端末：arrows We2 Plus。ARCore が利用できる実機を対象にする。
- PC：Windows、Radeon RX 7800 XT。Windows で GPU 固有機能がなくても処理できるようにする。
- 用途：Blender でスキャン結果を半透明の下絵として表示し、客室モデルを作る。
- 収録は端末上で逐次保存し、停止・クラッシュ・空き容量不足で既存フレームを壊さない。
- Raw Depth は疎で欠損する前提。RGB、ARCore 姿勢、深度、信頼度を同じフレーム ID とタイムスタンプで結び付ける。
- ARCore の姿勢は初期値として使い、PC 側で画像対応・ループ閉じ込み・深度整合を行う。
- 実寸スケールは ARCore のメートル座標を初期値にし、任意で既知距離を入力して補正できるようにする。

## 完成条件（実用版）

### Android Capture

- ARCore のトラッキング状態、カメラ姿勢、内部パラメータ、RGB、Raw Depth、Confidence、露出・焦点距離・ISO、端末情報を保存できる。
- 端末が Raw Depth に対応しない場合も、RGB＋姿勢収録として安全に完了し、処理側へ明確に通知する。
- 高解像度 CPU カメラストリームを端末が提示する範囲で選択し、30fps を基本にする。
- フレームを移動量・回転量・時間間隔・ブレ・露出で選別し、保存負荷と重複を抑える。
- 画面上に録画状態、トラッキング状態、保存枚数、空き容量、ブレ・速度警告、深度有効率、撮影軌跡を表示する。
- 一時フォルダへ書き続け、終了時に manifest とチェックサムを確定する。未完了収録も再開・破棄を選べる。
- `.roomcap` は共有・ファイルアプリ・USB 転送で PC に渡せるディレクトリまたは ZIP とする。

### Windows Processor

- `.roomcap` の manifest、ファイル欠落、サイズ、チェックサム、時刻、姿勢、内部パラメータを検査する。
- ブレ、暗すぎる/明るすぎる画像、重複フレーム、トラッキング不良をスコア化し、除外理由をレポートする。
- ARCore 姿勢を初期値として深度を統合し、開始地点へ戻った収録では明示指定により保守的なループ閉じ込み補正を行う。外部の画像 BA/COLMAP は入力経路を置き換えられる拡張点として残し、標準処理は外部ツールなしで完了する。
- RGB と信頼度付き Raw Depth を姿勢に従って統合し、外れ値を除去する。
- 床・壁・天井を推定し、座標系を床 XY、上方向 Z に整列する。既知距離で全体スケールを補正できる。
- 深度/点群から高密度の `room_reference_raw.glb` と、ノイズ除去・軽量化した `room_reference_clean.glb` を生成する。
- 撮影画像から面ごとのテクスチャを作り、色補正と重複投影の品質スコアを記録する。
- `cameras.json`、`pointcloud.ply`、`measurements.csv`、`quality_report.html` を同時に出力する。
- 途中生成物をキャッシュし、再実行時に完了済みの工程を再利用できる。ログとエラーをユーザーが読める形で出す。

### Blender 連携

- GLB を Blender に読み込んだ時、単位はメートル、床が XY、上方向が Z になっている。
- Raw / Clean が別オブジェクトまたは別コレクションとして識別できる。
- マテリアルとテクスチャが GLB 内に自己完結し、移動しても相対パスが壊れない。
- Blender 用の補助スクリプトで、参照コレクション作成、半透明化、カメラと元画像の登録、寸法情報の配置を行える。

## アーキテクチャ

```text
RoomTrace/
├── android/                  # Kotlin / ARCore 収録アプリ
├── web/                      # GitHub Pages向け静的WebXR収録SPA
├── processor/                # Python Windows CLI・GUI 入口
│   ├── roomtrace/             # 収録形式・検査・再構築・GLB 出力
│   └── tests/
├── blender/                  # Blender 3.x/4.x 用インポート補助
├── schemas/                  # manifest / frame metadata の JSON Schema
├── examples/                 # 小さな合成データと処理例
├── docs/                     # 使用方法、撮影手順、トラブルシュート
├── .github/workflows/        # GitHub Pages静的デプロイ
└── plans.md
```

PC 側は重い GUI フレームワークに依存しない CLI を正本にする。Windows では `roomtrace inspect`, `roomtrace process`, `roomtrace report` を使い、後から GUI は同じサービス層を呼び出す。これにより長時間処理、ログ保存、再実行、バッチ処理を安定させる。

## データ契約

```text
capture.roomcap/
├── manifest.json
├── images/00000001.jpg
├── depth/00000001.png       # uint16 millimetres, little-endian
├── confidence/00000001.png  # uint8 0..255
├── frames.jsonl             # frame_id, timestamps, pose, quality
├── intrinsics.json
├── trajectory.jsonl         # optional dense poses
└── checksums.sha256
```

RGB と Depth は同じ `frame_id` を使う。画像に Depth がないフレームも許容し、manifest の capabilities と各フレームの availability で表現する。姿勢は ARCore のワールド座標からカメラ座標への変換を明記し、Processor で曖昧に解釈しない。

## 開発順

1. **契約と検査**：スキーマ、データモデル、完全性検査、CLI、合成収録。
2. **PC の形状経路**：姿勢付き深度統合、外れ値除去、床面整列、点群/メッシュ生成。
3. **テクスチャと GLB**：可視面の投影、色補正、アトラス化、Raw/Clean 出力、Blender 検証。
4. **Android 収録**：ARCore セッション、キーフレーム選別、逐次保存、共有、撮影ガイド。
5. **品質と再実行**：キャッシュ、レポート、失敗復旧、ログ、テスト、Windows 手順。
6. **実機受け入れ**：We2 Plus で自宅一室を収録し、旅行前に解像度/fps/空き容量/処理時間/スケール誤差を測る。

## 品質基準

- Python の単体テストと合成収録による end-to-end テストを持つ。
- 不正データを「黙って補正」せず、致命的エラーと警告を分ける。
- 生成 GLB の参照画像・テクスチャ・単位・座標系を自動検査する。
- 大きな入力でも全画像を一度にメモリへ積まず、ストリーミング/タイル処理を優先する。
- 端末で未検証の機能はコード上で capability として扱い、実機未検証をドキュメントに残す。
- 旅行先でネットワークがなくても撮影・転送・PC 処理ができる。

## 完成後の利用手順

1. Android アプリを We2 Plus にインストールする。
2. 空き容量とバッテリーを確認し、ガイドに従って客室を一周、詳細、床、天井の順に撮影する。
3. 収録フォルダを Windows PC にコピーする。
4. `roomtrace inspect <capture>` で欠落・品質を確認する。
5. `roomtrace process <capture> --output <folder>` で Raw/Clean GLB とレポートを作る。
6. Blender で `room_reference_clean.glb` を基準に構造を作り、`room_reference_raw.glb` と元画像で細部を確認する。

## リスクと扱い

- **ARCore の姿勢ドリフト**：標準経路では明示指定したループ閉じ込み補正を使い、補正量をレポートする。画像 BA/COLMAP を使う場合は外部姿勢入力へ差し替えられる構成にする。
- **Raw Depth の欠損**：Confidence 閾値、複数フレーム統合、RGB の補助を使う。壁面が未観測なら「推測」で埋めず警告する。
- **鏡・窓・透明物**：低信頼領域として残し、メッシュの過剰な穴埋めを避ける。
- **AMD/Windows の GPU 差**：必須処理は CPU/OpenCV/Open3D 相当で完結し、GPU は任意最適化にする。
- **Android 実機差**：カメラ構成・Depth 対応・保存解像度を起動時に記録し、端末固有の決め打ちをしない。
- **Blender のバージョン差**：標準 glTF/GLB を正本にし、補助スクリプトはバージョン検査と安全な失敗を行う。

## 今回の開発で「完了」とするもの

- この計画に対応するソース、テスト、サンプル、ドキュメントがワークスペースに揃っている。
- 合成収録から Blender で開ける Raw/Clean GLB まで、実機なしで自動検証できる。
- Android アプリは ARCore 実機でビルド可能な構成とし、未検証部分を明記する。
- 実機での最終受け入れだけはこの環境から実行できないため、具体的な手順・期待値・ログ確認方法を残す。

## 実装状況

- [x] `.roomcap` v1 の manifest / frames / intrinsics / checksum 契約と検査
- [x] Windows 側の品質スコア、キーフレーム選別、深度メッシュ、床面整列、スケールアンカー
- [x] 埋め込み JPEG テクスチャ付き Raw GLB、Clean GLB、PLY、カメラ、寸法、HTML レポート
- [x] CLI、Tk ベースの簡易デスクトップ入口、合成収録、ZIP 入力、end-to-end テスト
- [x] Android の ARCore 収録、Raw Depth/Confidence、逐次保存、チェックサム ZIP、共有 UI
- [x] GitHub Pages向けサーバーレスSPA、IndexedDB一時保存、WebXR Depth取得、ブラウザ内 `.roomcap.zip` 生成
- [x] Blender インポート補助と Windows/Android/撮影・処理手順
- [x] Windows の `RoomTrace.cmd` 一回起動セットアップ、ドラッグ＆ドロップ、出力先自動決定、Blender起動ボタン
- [x] Android の `Build-APK.cmd` 一回ビルド導線（SDK/JDK検出、Gradle取得、APKコピー）
- [ ] We2 Plus実機でのAndroidアプリ版とブラウザ版のWebXR Depth対応・収録・処理・Blender受け入れ（この環境では実機がないため残置）
