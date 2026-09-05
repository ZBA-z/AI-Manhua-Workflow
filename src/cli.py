from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .assets import index_assets, match_by_prompt, normalize_name
from .parser import parse_script, validate_episodes
from .planner import plan_all, split_by_reference_limit
from .prompt_check import check_all
from .prompt_check import safe_rewrite
from .reports import write_report
from .setting_library import attach_specs_to_assets, load_setting_library
from .store import Store
from .ui_inspect import inspect_window
from .runner import archive_pending, audit_accounts, confirm_generated, download_pending, run_one, validate_one
from .startup import run_startup


def load_config(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def preflight_inputs(config: dict) -> tuple[list, list]:
    """Load production inputs and reject incomplete sources before state writes."""
    errors: list[str] = []
    required_dirs = ("script_dir", "character_dir", "scene_dir")
    for key in required_dirs:
        value = config.get(key)
        if not value:
            errors.append(f"配置缺少 {key}")
        elif not Path(value).is_dir():
            errors.append(f"目录不存在: {key}={value}")
    for index, value in enumerate(config.get("supplemental_asset_dirs", []), start=1):
        if not Path(value).is_dir():
            errors.append(f"补充资产目录不存在: supplemental_asset_dirs[{index}]={value}")
    for key in ("background_settings", "asset_settings"):
        value = config.get(key)
        if value and not Path(value).is_file():
            errors.append(f"设定文件不存在: {key}={value}")
    if config.get("download_dir_must_equal_output"):
        download_dir = Path(config.get("download_dir", "")).resolve()
        output_dir = Path(config.get("output_dir", "")).resolve()
        if download_dir != output_dir:
            errors.append(f"下载目录必须与成片目录相同: download_dir={download_dir}; output_dir={output_dir}")
    if errors:
        raise RuntimeError("输入预检失败:\n- " + "\n- ".join(errors))

    scripts = sorted(Path(config["script_dir"]).glob("*.txt"))
    if not scripts:
        errors.append(f"剧本目录中没有 .txt 文件: {config['script_dir']}")

    episodes = []
    for script in scripts:
        try:
            episodes.extend(parse_script(script))
        except Exception as exc:
            errors.append(f"剧本解析失败: {script.name}: {exc}")
    episodes.sort(key=lambda item: item.number)
    episode_numbers = [episode.number for episode in episodes]
    duplicates = sorted({number for number in episode_numbers if episode_numbers.count(number) > 1})
    if duplicates:
        errors.append("重复定义集数: " + ",".join(map(str, duplicates)))

    character_assets = index_assets(config["character_dir"], "character")
    scene_assets = index_assets(config["scene_dir"], "scene")
    supplemental_assets = []
    for extra_dir in config.get("supplemental_asset_dirs", []):
        supplemental_assets.extend(index_assets(extra_dir, "character"))

    validation = config.get("validation", {})
    episode_count = len(episodes)
    shot_count = sum(len(episode.shots) for episode in episodes)
    checks = (
        (episode_count, int(validation.get("min_episodes", 1)), "集数"),
        (shot_count, int(validation.get("min_shots", 1)), "镜头数"),
        (len(character_assets), int(validation.get("min_character_assets", 1)), "人物/道具资产数"),
        (len(scene_assets), int(validation.get("min_scene_assets", 1)), "场景资产数"),
    )
    for actual, minimum, label in checks:
        if actual < minimum:
            errors.append(f"{label}不足: 实际{actual}，最低要求{minimum}")

    expected_start = validation.get("expected_episode_start")
    expected_end = validation.get("expected_episode_end")
    if expected_start is not None and expected_end is not None:
        expected = set(range(int(expected_start), int(expected_end) + 1))
        actual = set(episode_numbers)
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        if missing:
            errors.append("缺少预期集数: " + ",".join(map(str, missing)))
        if unexpected:
            errors.append("出现范围外集数: " + ",".join(map(str, unexpected)))

    if errors:
        raise RuntimeError("输入预检失败:\n- " + "\n- ".join(errors))
    return episodes, character_assets + scene_assets + supplemental_assets


def prepare(config_path: str) -> None:
    config = load_config(config_path)
    video = config.get("video", {})
    target_seconds = int(video.get("duration_seconds", 10))
    episodes, assets = preflight_inputs(config)
    Path(config["output_dir"]).mkdir(parents=True, exist_ok=True)
    setting_library = load_setting_library(config.get("background_settings"), config.get("asset_settings"))
    fatal_errors = validate_episodes(episodes)
    warnings: list[str] = []
    seen: set[int] = set()
    for episode in episodes:
        if episode.number in seen:
            fatal_errors.append(f"第{episode.number}集在多个剧本文件中重复出现")
        seen.add(episode.number)
    for shot, prompt_issues in check_all([shot for episode in episodes for shot in episode.shots]):
        for item in prompt_issues:
            message = f"第{shot.episode}集镜号{shot.number}提示词[{item.severity}] {item.message}"
            if str(item.severity).lower() == "error":
                fatal_errors.append(message)
            else:
                warnings.append(message)
    # A supplementary asset with the same filename is an explicit replacement
    # for the older library entry. Deduplicate by filename before matching so
    # identical aliases cannot become an unresolved tie.
    assets = list({asset.name: asset for asset in assets}.values())
    attach_specs_to_assets(assets, setting_library)
    groups = plan_all(episodes, target_seconds=target_seconds)
    preliminary_refs: dict[tuple[int, str], set[str]] = {}
    for episode in episodes:
        for shot in episode.shots:
            paths = {asset.path for asset in match_by_prompt(shot.prompt, assets, config.get("reference_aliases", {}))}
            for number in shot.references:
                mapped = config.get("reference_map", {}).get(str(number))
                if isinstance(mapped, list):
                    paths.update(str(item) for item in mapped)
                elif mapped:
                    paths.add(str(mapped))
            preliminary_refs[(shot.episode, str(shot.number))] = paths
    groups = split_by_reference_limit(groups, preliminary_refs, int(config.get("max_reference_images", 10)), target_seconds)
    store = Store(config["database"])
    store.sync_accounts(config.get("accounts", []), datetime.now(timezone(timedelta(hours=8))).date().isoformat())
    store.sync_task_ids([group.task_id for group in groups])
    coverage = {"shots": 0, "shots_with_reference": 0, "shots_without_reference": 0, "missing_scene": [], "missing_character": [], "missing_prop": [], "unresolved": []}
    for group in groups:
        refs: list[str] = []
        shot_ref_paths: dict[str, list[str]] = {}
        shot_unresolved: dict[str, bool] = {}
        asset_warnings: list[str] = []
        group_has_unresolved = False
        for shot in group.shots:
            shot_has_unresolved = False
            coverage["shots"] += 1
            rewritten, changes = safe_rewrite(shot.prompt)
            if changes:
                store.event(group.task_id, "prompt", f"镜号{shot.number}安全改写：{'、'.join(changes)}")
            shot_paths: list[str] = []
            prompt_matches = match_by_prompt(shot.prompt, assets, config.get("reference_aliases", {}))
            if prompt_matches:
                shot_paths.extend(asset.path for asset in prompt_matches)
                if len(prompt_matches) > 4:
                    asset_warnings.append(f"镜号{shot.number}自动匹配资产过多({len(prompt_matches)})")
            if shot.references:
                for number in shot.references:
                    mapped = config.get("reference_map", {}).get(str(number))
                    if isinstance(mapped, list):
                        shot_paths.extend(str(item) for item in mapped)
                    else:
                        shot_paths.append(str(mapped) if mapped else f"UNMAPPED_REFERENCE_{number}")
            elif not prompt_matches:
                asset_warnings.append(f"镜号{shot.number}未从提示词匹配到资产")
            no_reference_allowlist = tuple(config.get("no_reference_allowlist_terms", ("黑场", "纯黑", "抽象画面", "能量漩涡", "宇宙深处")))
            no_reference_allowed = any(term in shot.prompt for term in no_reference_allowlist)
            if not shot_paths and not no_reference_allowed:
                asset_warnings.append(f"镜号{shot.number}无参考图且不在无参考图白名单")
                coverage["unresolved"].append({"episode": shot.episode, "shot": shot.number, "category": "reference", "reason": "没有参考图且缺少白名单理由"})
                shot_has_unresolved = True
                group_has_unresolved = True
            categories = {asset.category for asset in prompt_matches}
            # Only concrete location phrases create a missing-scene finding.
            # Broad words such as "城市" or "室内" describe composition, not
            # an asset identity, and previously produced many false positives.
            scene_terms = (
                "临时营地", "营地公园", "避难营地", "医院大厅", "医院外",
                "城市中心广场", "时间倒流", "蓝星超远景", "蓝星大气层",
                "宇宙深处", "外太空", "战舰机库", "舰桥", "基地外",
                "基地密室", "基地废墟", "训练室", "演习场", "战舰会议室",
                "指挥中心", "混沌世界", "宇宙创生", "封印深处",
                "诡异占领区", "红雾污染", "城市废墟", "战壕", "神照社",
                "海底神殿", "海底遗迹", "地下避难所", "地下车库", "大教堂",
            )
            if any(term in shot.prompt for term in scene_terms) and "scene" not in categories and not any("UNMAPPED_REFERENCE_" in path for path in shot_paths):
                asset_warnings.append(f"镜号{shot.number}检测到场景实体但未匹配场景资产")
                coverage["missing_scene"].append(f"第{shot.episode}集镜号{shot.number}")
                coverage["unresolved"].append({"episode": shot.episode, "shot": shot.number, "category": "scene", "reason": "检测到场景词但没有匹配文件"})
                shot_has_unresolved = True
                group_has_unresolved = True
            character_terms = ("南宫镜", "林雾", "周峥", "陈默", "陈刚", "苏媚娘", "马天霸", "哪吒", "伏羲", "荷鲁斯", "洛基", "阿撒托斯", "时烬", "沈毅", "星噬")
            # Check each named entity independently. A different character in
            # the same shot must not mask a missing dedicated reference.
            for term in character_terms:
                if term in shot.prompt and not any(a.category == "character" for a in match_by_prompt(term, assets, config.get("reference_aliases", {}))):
                    coverage["missing_character"].append(f"第{shot.episode}集镜号{shot.number}")
                    coverage["unresolved"].append({"episode": shot.episode, "shot": shot.number, "category": "character", "entity": term, "reason": "检测到角色名但没有匹配文件"})
                    shot_has_unresolved = True
                    group_has_unresolved = True
            # "渊" is intentionally not matched by the existing 渊族 asset:
            # the script describes a distinct young-girl character.
            if any(token in shot.prompt for token in ("（渊", "(渊", "女孩，渊")):
                dedicated_yuan = any(normalize_name(Path(a.name).stem) in ("渊", "渊安全", "渊安全版") and a.category == "character" for a in assets)
                if not dedicated_yuan:
                    coverage["missing_character"].append(f"第{shot.episode}集镜号{shot.number}")
                    coverage["unresolved"].append({"episode": shot.episode, "shot": shot.number, "category": "character", "entity": "渊", "reason": "需要渊的专属人物参考图，不能使用渊族.png替代"})
                    shot_has_unresolved = True
                    group_has_unresolved = True
            prop_terms = ("金属匣子", "密码匣", "军旗", "魔药", "污染结晶", "心脏化石", "盘古心脏", "遗骸", "手术刀", "火尖枪", "风火轮", "混天绫", "乾坤圈", "紫微帝玺", "天枢剑", "伏羲琴", "荷鲁斯之眼", "Was权杖", "谎言之角", "谎言号角", "变形法杖", "双柄酒杯", "常春藤权杖", "高频短刃", "蛊虫袋", "黄符", "全息地图", "反物质导弹", "开天巨斧", "巨斧", "照片", "地图")
            for term in prop_terms:
                if term in shot.prompt and not any(a.category == "prop" for a in match_by_prompt(term, assets, config.get("reference_aliases", {}))):
                    coverage["missing_prop"].append(f"第{shot.episode}集镜号{shot.number}")
                    coverage["unresolved"].append({"episode": shot.episode, "shot": shot.number, "category": "prop", "entity": term, "reason": "检测到道具词但没有匹配文件"})
                    shot_has_unresolved = True
                    group_has_unresolved = True
            unique_paths = []
            for path in shot_paths:
                if path not in unique_paths:
                    unique_paths.append(path)
                if path not in refs:
                    refs.append(path)
            shot_ref_paths[shot.number] = unique_paths
            shot_unresolved[shot.number] = shot_has_unresolved
            if unique_paths:
                coverage["shots_with_reference"] += 1
            else:
                coverage["shots_without_reference"] += 1
        prompt_lines: list[str] = []
        ref_index = {path: index + 1 for index, path in enumerate(refs)}
        if refs:
            labels = [f"参考图{index}={Path(path).name}" for path, index in ref_index.items()]
            prompt_lines.append("本组上传参考图清单：" + "；".join(labels))
            prompt_lines.append("分镜中的人物身份、道具身份和动作服从分镜文字；所有实体的脸型、发型、服装、材质、颜色和外观细节以对应参考图为准，不按文字重绘或替换外观。")
        for shot in group.shots:
            rewritten, _ = safe_rewrite(shot.prompt)
            duration = group.duration_map.get(shot.number, shot.duration)
            shot_numbers = [str(ref_index[path]) for path in shot_ref_paths.get(shot.number, []) if path in ref_index]
            if shot_numbers:
                reference_note = f"；本镜使用参考图：{','.join(shot_numbers)}"
            elif shot_unresolved.get(shot.number, False):
                reference_note = "；本镜存在未匹配实体，需补充专属资产"
            elif config.get("allow_no_reference_shots", False):
                reference_note = "；本镜允许无参考图（黑场或抽象画面）"
            else:
                reference_note = "；本镜无可确认参考图，需人工复核"
            prompt_lines.append(f"镜号{shot.number}，时长约{duration}s{reference_note}；{rewritten}")
        prompt = "\n".join(prompt_lines)
        max_refs = int(config.get("max_reference_images", 8))
        if group_has_unresolved:
            group.status = "manual"
            group.adjustment = (group.adjustment + "；存在未匹配实体，补齐专属资产后再提交").lstrip("；")
            store.event(group.task_id, "asset", "存在未匹配人物/场景/道具，阻断生成")
        if len(refs) > max_refs:
            group.status = "manual"
            group.adjustment = f"参考图{len(refs)}张超过上限{max_refs}，需拆组或制作合集图后再提交"
            store.event(group.task_id, "asset", group.adjustment)
        manual_reason = group.adjustment or "需要人工复核" if group.status == "manual" else None
        store.upsert_group(group, prompt, refs, model=str(video.get("model", "Seedance 2.0 Fast")), duration_seconds=target_seconds, aspect_ratio=str(video.get("aspect_ratio", "16:9")), manual_reason=manual_reason)
        if any(value.startswith("UNMAPPED_REFERENCE_") for value in refs):
            store.event(group.task_id, "asset", "参考图编号未映射；请在配置中补充 reference_map 或人工选择")
        for warning in asset_warnings:
            store.event(group.task_id, "asset", warning)
            warnings.append(f"{group.task_id} {warning}")
    for key in ("missing_scene", "missing_character", "missing_prop"):
        coverage[key] = sorted(set(coverage[key]))
    coverage["unresolved"] = sorted({(item["episode"], str(item["shot"]), item["category"], item.get("entity", "")): item for item in coverage["unresolved"]}.values(), key=lambda item: (item["episode"], str(item["shot"]), item["category"], item.get("entity", "")))
    queue_status_counts: dict[str, int] = {}
    for row in store.tasks():
        queue_status_counts[row["status"]] = queue_status_counts.get(row["status"], 0) + 1
    manual_actions = [
        {"task_id": group.task_id, "episode": group.episode, "reason": group.adjustment or "需要人工复核"}
        for group in groups if group.status == "manual"
    ]
    write_report(config["report"], episodes, groups, fatal_errors, parameters=video, setting_library=setting_library.summary, asset_coverage=coverage, text_agent=config.get("text_agent"), queue_status_counts=queue_status_counts, warnings=warnings, manual_actions=manual_actions)
    print(
        f"prepared episodes={len(episodes)} shots={sum(len(e.shots) for e in episodes)} "
        f"groups={len(groups)} fatal_errors={len(fatal_errors)} warnings={len(warnings)} "
        f"manual={len(manual_actions)} unresolved={len(coverage['unresolved'])}"
    )


def report(config_path: str) -> None:
    config = load_config(config_path)
    print(Path(config["report"]).read_text(encoding="utf-8"))


def resume_paused(config_path: str) -> None:
    """Requeue only transient UI-paused tasks after the desktop is fixed."""
    config = load_config(config_path)
    store = Store(config["database"])
    paused = [row["task_id"] for row in store.tasks() if row["status"] == "paused"]
    for task_id in paused:
        store.set_status(task_id, "ready", None)
        store.event(task_id, "workflow", "人工确认 UI 已恢复，任务重新排队；审核拦截任务不会自动恢复")
    print(f"resumed_paused={len(paused)}")


def startup(config_path: str) -> None:
    result = run_startup(config_path, prepare_callback=prepare)
    print(json.dumps({
        "run_id": result.run_id,
        "state": result.state,
        "semantic": result.semantic,
        "exit_code": result.exit_code,
    }, ensure_ascii=False))
    raise SystemExit(result.exit_code)


def main() -> None:
    parser = argparse.ArgumentParser(prog="ai-manhua-workflow")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, handler in (("prepare", prepare), ("report", report), ("resume-paused", resume_paused), ("download-pending", download_pending), ("audit-accounts", audit_accounts), ("startup", startup)):
        command = sub.add_parser(name)
        command.add_argument("--config", required=True)
        command.set_defaults(handler=handler)
    inspect = sub.add_parser("inspect-ui")
    inspect.add_argument("--output", default="data/ui-controls.json")
    inspect.set_defaults(handler=lambda output: print(f"controls={inspect_window(output)} output={output}"))
    run = sub.add_parser("run-one")
    run.add_argument("--config", required=True)
    run.set_defaults(handler=lambda config: print(run_one(config)))
    validate = sub.add_parser("validate-one")
    validate.add_argument("--config", required=True)
    validate.set_defaults(handler=lambda config: print(validate_one(config)))
    confirm = sub.add_parser("confirm-generated")
    confirm.add_argument("--config", required=True)
    confirm.add_argument("--task-id", required=True)
    confirm.add_argument("--account", required=True)
    confirm.add_argument("--output-path")
    confirm.set_defaults(handler=lambda config, task_id, account, output_path: print(confirm_generated(config, task_id, account, output_path)))
    archive = sub.add_parser("archive-pending")
    archive.add_argument("--config", required=True)
    archive.add_argument("--task-id", required=True)
    archive.add_argument("--source-path", required=True)
    archive.set_defaults(handler=lambda config, task_id, source_path: print(archive_pending(config, task_id, source_path)))
    args = parser.parse_args()
    if args.command == "inspect-ui":
        args.handler(args.output)
    elif args.command == "confirm-generated":
        args.handler(args.config, args.task_id, args.account, args.output_path)
    elif args.command == "archive-pending":
        args.handler(args.config, args.task_id, args.source_path)
    elif args.command in {"download-pending", "audit-accounts"}:
        print(args.handler(args.config))
    else:
        args.handler(args.config)


if __name__ == "__main__":
    main()
