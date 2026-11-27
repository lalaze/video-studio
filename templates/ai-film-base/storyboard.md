# 灯塔的清晨（示例） · storyboard

<!-- 由 scripts/ai/film.py plan --write 从 prompts/shots.json 生成；改分镜请改 shots.json 再重新生成。 -->

12.5 s · 1920×1080 · 24 fps · 16:9 · 3 镜。正式片：分镜确认后再出关键帧。

关键帧风格：Cinematic film still, 35mm look, soft natural light, muted teal and warm amber palette, gentle film grain, no text, no watermark

视频风格：Live-action, cinematic, 35mm film look, soft natural light, muted teal and warm amber palette

## 参考图（人物 / 风格设定）

| 名字 | 说明 | 提示词 |
|---|---|---|
| `keeper` | 设定图：七十多岁的守塔人，白色短胡子，饱经风霜但和善的脸，藏青色毛衣、深色帆布裤、旧皮靴；正面和四分之三侧面并排，暖灰色纯背景 | Character reference sheet of one elderly lighthouse keeper in his seventies, short white beard, weathered kind face, navy wool sweater, dark canvas trousers, worn leather boots. The same man shown twice side by side, front view and three-quarter view, full body, standing on a plain warm grey studio background, even soft lighting |

## 分镜

| # | 时间 | 画面说明 | 动作说明 | 台词 | 转场 |
|---|---|---|---|---|---|
| s01 | 0.0 – 4.0 | 远景定场：黎明前的蓝色时刻，礁石岬角上一座白色小灯塔，海面平静发暗，天边一线暖光；塔灯还没亮，只有塔底一扇窗亮着 | 镜头缓缓推近灯塔，海浪轻拍礁石，天边慢慢变亮。声音：海浪、低沉的海风、远处一声海鸥 | 天还没亮，灯塔已经醒了。 | fade |
| s02 | 3.5 – 7.5 | 中景，灯塔内部：守塔人（参考图 keeper）提着铜油灯爬窄窄的螺旋铁楼梯，灯光从下往上照亮他的脸，身后小圆窗透进冷蓝色的光 | 镜头跟着他慢慢上摇，油灯轻轻摇晃，暖光扫过弧形石墙。声音：铁台阶上缓慢的脚步声、油灯提手轻响、窗外微弱的风声 | 他每天第一个上楼。 | cut |
| s03 | 7.5 – 12.5 | 特写：巨大的玻璃菲涅尔透镜从内部亮起琥珀色暖光，一圈圈棱纹反着光，透过灯室玻璃能看到第一缕日出 | 镜头从透镜慢慢拉远，光越来越亮，身后的日出越来越宽。声音：灯点亮时低沉的电流嗡声、风压在灯室玻璃上 | 等第一缕光来接班。 | cut |

## 给模型的提示词

### s01

- 画面：Wide establishing shot of a small white lighthouse on a rocky headland at blue hour just before dawn, calm dark sea, a faint warm glow on the horizon, the lighthouse lamp still off, one lit window at its base
- 运镜：push-in
- 动作：the small white lighthouse shown in &lt;Picture 1&gt; stands on the rocky headland at blue hour, keeping its shape, the dark sea and the single lit window at its base. The camera pushes in with small amplitude at slow speed toward the lighthouse as gentle waves roll against the rocks and the warm glow on the horizon slowly brightens.
- 声音：Gentle waves wash over the rocks under a low, steady sea wind. A distant gull calls once.

### s02

- 画面：Medium shot inside the lighthouse: the old keeper from &lt;Picture 1&gt; climbs a narrow spiral iron staircase, holding a brass oil lantern that lights his face from below, cool blue light from a small round window behind him
- 参考图：`keeper`
- 运镜：tilt-up
- 动作：the old lighthouse keeper shown in &lt;Picture 1&gt; keeps his white beard, navy wool sweater and the brass oil lantern in his hand as he climbs the narrow spiral iron staircase. The camera tilts up at slow speed, following him; the lantern sways slightly and its warm light slides across the curved stone wall.
- 声音：Slow footsteps ring on the iron steps and the lantern handle creaks softly. A faint wind hums outside the small round window.

### s03

- 画面：Close-up of a huge glass Fresnel lighthouse lens beginning to glow warm amber from inside, its ribbed rings catching the light, the first sliver of sunrise visible on the horizon through the lantern-room windows
- 运镜：pull-out
- 动作：the huge glass Fresnel lens shown in &lt;Picture 1&gt; glows warm amber from inside, its ribbed rings catching the light. The camera pulls out with small amplitude at slow speed as the glow grows brighter and the first sliver of sunrise widens on the horizon behind the lantern-room windows.
- 声音：A low electric hum rises as the lamp warms up, with soft wind pressing against the lantern-room glass.
