"""部署预检：不等到晚上，白天也能验证配置对不对。

背景：main.py 只在**北京时间 21:00–23:59** 才执行，其他时间会静默退出。
所以白天 `python main.py` 什么都测不出来。本脚本跳过时间窗口判断，
依次验证：认证（token 或学号密码）→ 今日计划 init → 经纬度是否命中校区。

用法（放到仓库根目录，与 src/ 同级）：
    python preflight.py            # 只检查，不打卡
    python preflight.py --do       # 真的提交打卡 —— 仅限 21:00–23:59 之间！

退出码：0 = 通过；1 = 有环节失败（原因见输出）。
"""
import json
import sys

import requests

from src.checkin import AttnClient, beijing_now, do_checkin as run_checkin, query_today_task
from src.config import load_config


def _mask(s: str) -> str:
    s = str(s or "")
    if len(s) <= 4:
        return "*" * len(s)
    return f"{s[:2]}{'*' * (len(s) - 4)}{s[-2:]}"


def main() -> int:
    do_checkin = "--do" in sys.argv
    failed = False

    cfg = load_config()
    user_cfg = cfg.get("user") or {}
    chk = cfg.get("checkin") or {}
    now = beijing_now()
    in_window = (21, 0) <= (now.hour, now.minute) and now.hour <= 23

    print(f"当前北京时间：{now:%Y-%m-%d %H:%M}（{'在' if in_window else '不在'}签到窗口内）")
    if do_checkin and not in_window:
        print("!! --do 只能在 21:00–23:59 使用，否则服务端会报『不在签到时段』。已中止。")
        return 1
    print("-" * 56)

    # 1. 认证
    token = (user_cfg.get("token") or "").strip()
    if token:
        print(f"[1/4] 认证：使用 config.yaml 中的 token（{_mask(token)}）")
    else:
        u, p = user_cfg.get("username"), user_cfg.get("password")
        if not (u and p):
            print("[1/4] 认证：✗ 既没有 token，也没有学号/密码")
            return 1
        print(f"[1/4] 认证：学号 {_mask(u)} 走 sso.fzu.edu.cn 登录 ...")
        try:
            from src.login import login

            token = login(u, p)
            print(f"      ✓ 登录成功，拿到 token（{_mask(token)}）")
        except Exception as e:
            print(f"      ✗ 登录失败：{e}")
            print("      常见原因：密码不是 App 当前密码 / SSO 要求验证码 / 登录页结构变化。")
            print("      可退化为 token 方式：Android 执行")
            print('        adb logcat -d | grep -oE "index\\.action\\?token=[A-Za-z0-9]+" | tail -1')
            print("      （先在手机上打开一次晚点名页面），把 token 填进 config.yaml 的 user.token")
            return 1

    client = AttnClient(token)

    # 2. 今日计划
    print("[2/4] 查询今日计划（init.action）...")
    try:
        status = query_today_task(client, cfg)
    except requests.HTTPError as e:
        print(f"      ✗ 服务器返回 HTTP 错误：{e}")
        print("      跨午夜时段（约 0:00–1:00）服务器尚未发布当日计划属正常，稍后再试。")
        return 1
    except Exception as e:
        print(f"      ✗ 查询失败：{e}")
        print("      token 可能已过期 → 改用学号密码方式，让它自动续期。")
        return 1

    init = status.get("init") or {}
    user = init.get("userData") or {}
    params = init.get("paramsData") or {}
    print(f"      ✓ 查到数据：FState={user.get('FState')!r}，"
          f"need_checkin={status['need_checkin']}")
    print(f"      签到时段 {params.get('FStartTime')} ~ {params.get('FEndTime')}"
          f"（迟到线 {params.get('FLateTime')}），今日状态 {user.get('FState')}")

    # 3. 定位校验
    print("[3/4] 定位校验（check.action）...")
    polygons = []
    for sd in init.get("schoolData") or []:
        try:
            polygons.extend(json.loads(sd["FPosition"]).get("range") or [])
        except Exception:
            continue
    if not polygons:
        print("      - 今日无考勤计划（schoolData 为空），无签到范围可校验。")
        print("        若今天确实应该有晚点名，稍后重试；否则属正常（无晚点名日）。")
    else:
        try:
            lon = float(chk["longitude"])
            lat = float(chk["latitude"])
        except (KeyError, TypeError, ValueError):
            print(f"      ✗ config.yaml 的 checkin.longitude/latitude 不是有效数字："
                  f"{chk.get('longitude')!r} / {chk.get('latitude')!r}")
            return 1
        try:
            matched = client.check(lon, lat, {"range": polygons})
        except Exception as e:
            print(f"      ✗ 定位校验请求失败：{e}")
            return 1
        if not matched:
            print(f"      ✗ 坐标 ({lon}, {lat}) 未命中任何校区范围")
            print("        去 https://lbs.amap.com/tools/picker 重新点选宿舍楼，")
            print("        复制「经度,纬度」，注意经度在前、纬度在后。")
            failed = True
        else:
            first = matched[0] or {}
            print(f"      ✓ 命中校区：{first.get('name') or first.get('id') or first}")

    # 4. 可选：真打卡
    if failed:
        print("[4/4] 跳过打卡（前置检查未通过）")
        return 1
    if not do_checkin:
        print("[4/4] 未指定 --do，不做真实打卡。")
        print("-" * 56)
        print("预检通过 ✅ 可以把 config.yaml 全文粘进 GitHub Secret（CONFIG_YAML）了。")
        print("如需在窗口内实测真实打卡：python preflight.py --do")
        return 0

    print("[4/4] 提交打卡（clockIn.action）...")
    if not status.get("need_checkin"):
        print(f"      今日已签到（FState={user.get('FState')}），无需重复打卡。")
        return 0
    try:
        ok = run_checkin(client, cfg, init)
    except Exception as e:
        print(f"      ✗ 打卡失败：{e}")
        return 1
    print("      ✓ 提交成功，去 App 里确认一下。" if ok else "      ✗ 定位未命中，服务端未受理。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
