"""Semantic workbench JS transforms for OpenAI BYOK routing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Match

from .layout import PatcherError

PATCH_MARKER = "[cursor-fix-openai-routing]"
OPENAI_MODEL_LITERAL = r"/^(?:gpt(?:-|$)|chatgpt(?:-|$)|o[134](?:-|$)|codex(?:-|$))/"
OPENAI_MODEL_PATTERN_RE = re.escape(OPENAI_MODEL_LITERAL)

IDENT = r"[$A-Za-z_][$\w]*"

CLASSIFIER_RE = re.compile(
    rf"""
    function[ ](?P<function>{IDENT})\(
      (?P<model>{IDENT}),(?P<settings>{IDENT})
    \)\{{return[ ]
      (?P<claude>{IDENT})\((?P=model)\)
      \?(?P=settings)\.useClaudeKey\?"anthropic":void[ ]0:
      (?P<google>{IDENT})\((?P=model)\)
      \?(?P=settings)\.useGoogleKey\?"google":void[ ]0:
      (?P=settings)\.useOpenAIKey\?"openai":void[ ]0
    \}}
    """,
    re.VERBOSE,
)

PATCHED_CLASSIFIER_RE = re.compile(
    rf"""
    function[ ](?P<function>{IDENT})\(
      (?P<model>{IDENT}),(?P<settings>{IDENT})
    \)\{{return[ ]
      (?P<claude>{IDENT})\((?P=model)\)
      \?(?P=settings)\.useClaudeKey\?"anthropic":void[ ]0:
      (?P<google>{IDENT})\((?P=model)\)
      \?(?P=settings)\.useGoogleKey\?"google":void[ ]0:
      (?P=settings)\.useOpenAIKey&&
      \(\((?P=settings)\.aiSettings\?\.userAddedModels\?\.includes\((?P=model)\)\?\?!1\)
      \|\|{OPENAI_MODEL_PATTERN_RE}\.test\((?P=model)\)\)
      \?"openai":void[ ]0
    \}}
    """,
    re.VERBOSE,
)

MODEL_DETAILS_RE = re.compile(
    rf"""
    getModelDetailsFromName\(
      (?P<model>{IDENT}),(?P<max>{IDENT})
    \)\{{let[ ](?P<key>{IDENT})=
      this\._cursorAuthenticationService\.getApiKeyForModel\((?P=model)\);
    const[ ](?P<enabled>{IDENT})=
      this\._aiSettingsService\.getUseApiKeyForModel\((?P=model)\),
      (?P<azure>{IDENT})=
      this\._reactiveStorageService\.applicationUserPersistentStorage\.azureState,
      (?P<bedrock>{IDENT})=
      this\._reactiveStorageService\.applicationUserPersistentStorage\.bedrockState;
    \(!(?P=enabled)\|\|!(?P=key)\)&&\((?P=key)=void[ ]0\);
    const[ ](?P<server>{IDENT})=
      this\._aiSettingsService\.getServerModelName\((?P=model)\);
    return[ ]new[ ](?P<ctor>{IDENT})\(\{{apiKey:(?P=key),
      modelName:(?P=server),azureState:(?P=azure),
      openaiApiBaseUrl:
      this\._reactiveStorageService\.applicationUserPersistentStorage\.openAIBaseUrl
      \?\?void[ ]0,
      bedrockState:(?P=bedrock),maxMode:(?P=max)\}}\)
    \}}
    """,
    re.VERBOSE,
)

PATCHED_MODEL_DETAILS_RE = re.compile(
    rf"""
    getModelDetailsFromName\(
      (?P<model>{IDENT}),(?P<max>{IDENT})
    \)\{{let[ ](?P<key>{IDENT})=
      this\._cursorAuthenticationService\.getApiKeyForModel\((?P=model)\);
    const[ ](?P<enabled>{IDENT})=
      this\._aiSettingsService\.getUseApiKeyForModel\((?P=model)\),
      (?P<azure>{IDENT})=
      this\._reactiveStorageService\.applicationUserPersistentStorage\.azureState,
      (?P<bedrock>{IDENT})=
      this\._reactiveStorageService\.applicationUserPersistentStorage\.bedrockState;
    \(!(?P=enabled)\|\|!(?P=key)\)&&\((?P=key)=void[ ]0\);
    const[ ](?P<server>{IDENT})=
      this\._aiSettingsService\.getServerModelName\((?P=model)\),
      (?P<base>{IDENT})=(?P=key)\?
      this\._reactiveStorageService\.applicationUserPersistentStorage\.openAIBaseUrl
      \?\?void[ ]0:void[ ]0
      (?:,(?P<provider_var>{IDENT})=(?P<provider_fn>{IDENT})\(
        (?P=model),this\._reactiveStorageService\.applicationUserPersistentStorage
      \))?;
    return[ ](?:console\.info\("{re.escape(PATCH_MARKER)}"[^\n]*?\),)?
      new[ ](?P<ctor>{IDENT})\(\{{apiKey:(?P=key),
      modelName:(?P=server),azureState:(?P=azure),
      openaiApiBaseUrl:(?P=base),
      bedrockState:(?P=bedrock),maxMode:(?P=max)\}}\)
    \}}
    """,
    re.VERBOSE,
)


@dataclass
class BundlePlan:
    path: Path
    original: str
    patched: str
    state: str


def classifier_replacement(match: Match[str]) -> str:
    g = match.groupdict()
    model, settings = g["model"], g["settings"]
    return (
        f'function {g["function"]}({model},{settings}){{return '
        f'{g["claude"]}({model})?{settings}.useClaudeKey?"anthropic":void 0:'
        f'{g["google"]}({model})?{settings}.useGoogleKey?"google":void 0:'
        f"{settings}.useOpenAIKey&&"
        f"(({settings}.aiSettings?.userAddedModels?.includes({model})??!1)||"
        f"{OPENAI_MODEL_LITERAL}.test({model}))?"
        f'"openai":void 0}}'
    )


def model_details_replacement(match: Match[str], trace: bool) -> str:
    g = match.groupdict()
    model, key = g["model"], g["key"]
    base = unique_identifier(match.string, "cursorFixBase")
    provider = unique_identifier(match.string, "cursorFixProvider")
    prefix = (
        f"getModelDetailsFromName({model},{g['max']}){{let {key}="
        f"this._cursorAuthenticationService.getApiKeyForModel({model});"
        f"const {g['enabled']}=this._aiSettingsService.getUseApiKeyForModel({model}),"
        f"{g['azure']}=this._reactiveStorageService.applicationUserPersistentStorage.azureState,"
        f"{g['bedrock']}=this._reactiveStorageService.applicationUserPersistentStorage.bedrockState;"
        f"(!{g['enabled']}||!{key})&&({key}=void 0);"
        f"const {g['server']}=this._aiSettingsService.getServerModelName({model}),"
        f"{base}={key}?this._reactiveStorageService.applicationUserPersistentStorage."
        f"openAIBaseUrl??void 0:void 0"
    )
    if trace:
        prefix += (
            f",{provider}={g['function'] if 'function' in g else 'undefined'}"
            if False
            else ""
        )
        trace_code = (
            f'console.info("{PATCH_MARKER}",{{modelName:{model},'
            f"apiKeyAttached:!!{key},openaiApiBaseUrlAttached:!!{base}}}),"
        )
    else:
        trace_code = ""
    return (
        prefix
        + ";return "
        + trace_code
        + f"new {g['ctor']}({{apiKey:{key},modelName:{g['server']},"
        f"azureState:{g['azure']},openaiApiBaseUrl:{base},"
        f"bedrockState:{g['bedrock']},maxMode:{g['max']}}})}}"
    )


def unique_identifier(text: str, stem: str) -> str:
    candidate = stem
    number = 2
    while re.search(rf"(?<![$\w]){re.escape(candidate)}(?![$\w])", text):
        candidate = f"{stem}{number}"
        number += 1
    return candidate


def plan_bundle(path: Path, trace: bool) -> BundlePlan:
    text = path.read_text(encoding="utf-8")
    vulnerable_classifiers = list(CLASSIFIER_RE.finditer(text))
    patched_classifiers = list(PATCHED_CLASSIFIER_RE.finditer(text))
    vulnerable_details = list(MODEL_DETAILS_RE.finditer(text))
    patched_details = list(PATCHED_MODEL_DETAILS_RE.finditer(text))

    if not vulnerable_classifiers and not vulnerable_details:
        if len(patched_classifiers) == 1 and len(patched_details) == 1:
            return BundlePlan(path, text, text, "already-patched")
        raise PatcherError(
            f"{path.name}: unsupported or ambiguous bundle "
            f"(classifier vulnerable={len(vulnerable_classifiers)}, "
            f"patched={len(patched_classifiers)}; model-details "
            f"vulnerable={len(vulnerable_details)}, patched={len(patched_details)})"
        )
    if len(vulnerable_classifiers) != 1 or len(vulnerable_details) != 1:
        raise PatcherError(
            f"{path.name}: refusing partial/ambiguous patch "
            f"(classifier={len(vulnerable_classifiers)}, "
            f"model-details={len(vulnerable_details)})"
        )

    patched = CLASSIFIER_RE.sub(classifier_replacement, text, count=1)
    patched = MODEL_DETAILS_RE.sub(
        lambda match: model_details_replacement(match, trace), patched, count=1
    )
    if patched == text:
        raise PatcherError(f"{path.name}: patch unexpectedly made no changes")
    if len(PATCHED_CLASSIFIER_RE.findall(patched)) != 1:
        raise PatcherError(f"{path.name}: patched classifier failed validation")
    if len(PATCHED_MODEL_DETAILS_RE.findall(patched)) != 1:
        raise PatcherError(f"{path.name}: patched model-details failed validation")
    return BundlePlan(path, text, patched, "needs-patch")
