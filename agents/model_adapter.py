"""
model_adapter.py — Model-Agnostic Bedrock Invocation Layer
===========================================================
Abstracts away the differences between:
  - Anthropic Claude (all versions)
  - Amazon Nova (Pro, Lite, Micro)
  - Amazon Titan
  - Meta Llama (via Bedrock)
  - Mistral (via Bedrock)

Handles:
  1. Request body formatting (each provider has a different schema)
  2. Inference profile ARN resolution (required for Claude Opus 4+,
     Claude 3.5 Sonnet v2, and other newer models)
  3. Response parsing (each provider returns a different response shape)
  4. Automatic retry with exponential backoff on throttling

Usage
-----
    adapter = BedrockModelAdapter(region_name="us-east-1")
    response = adapter.invoke(
        model_id = "anthropic.claude-3-sonnet-20240229-v1:0",
        prompt   = "Analyse this incident...",
        max_tokens = 1024,
    )
    print(response)  # always a plain string
"""

import json
import logging
import time
import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Model family detection
# ─────────────────────────────────────────────────────────────────────────────

def detect_model_family(model_id: str) -> str:
    """
    Detect which provider/family a model ID belongs to.

    Returns one of: 'claude', 'nova', 'titan', 'llama', 'mistral', 'unknown'
    """
    mid = model_id.lower()
    if "claude" in mid:
        return "claude"
    if "nova" in mid:
        return "nova"
    if "titan" in mid:
        return "titan"
    if "llama" in mid or "meta" in mid:
        return "llama"
    if "mistral" in mid:
        return "mistral"
    return "unknown"


def needs_inference_profile(model_id: str) -> bool:
    """
    Returns True for models that require a cross-region inference profile
    instead of direct on-demand invocation.

    As of 2026, this applies to:
      - Claude Opus 4.x and above
      - Claude 3.5 Sonnet v2 and above
      - Any model ID containing 'us.' prefix (already a profile)
    """
    mid = model_id.lower()
    # Already an inference profile ARN or cross-region prefix
    if mid.startswith("arn:") or mid.startswith("us.") or mid.startswith("eu."):
        return False
    # Models known to require inference profiles
    requires_profile = [
        "claude-opus-4",
        "claude-3-5-sonnet-20241022",
        "claude-3-5-haiku",
        "claude-opus-4-1",
    ]
    return any(pattern in mid for pattern in requires_profile)


def resolve_model_id(model_id: str, region: str = "us-east-1") -> str:
    """
    Convert a bare model ID to the correct invocation target.
    For models requiring inference profiles, prepends the cross-region prefix.
    """
    if needs_inference_profile(model_id):
        # Map region to cross-region prefix
        region_prefix_map = {
            "us-east-1": "us",
            "us-west-2": "us",
            "eu-west-1": "eu",
            "eu-central-1": "eu",
            "ap-southeast-1": "ap",
            "ap-northeast-1": "ap",
        }
        prefix = region_prefix_map.get(region, "us")
        resolved = f"{prefix}.{model_id}"
        logger.info("Model %s requires inference profile → resolved to %s", model_id, resolved)
        return resolved
    return model_id


# ─────────────────────────────────────────────────────────────────────────────
# Request body builders (one per provider family)
# ─────────────────────────────────────────────────────────────────────────────

def build_claude_body(prompt: str, max_tokens: int, system_prompt: str = "") -> dict:
    """Anthropic Claude — Messages API (all Claude 3+ models)."""
    body: dict = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens":        max_tokens,
        "messages":          [{"role": "user", "content": prompt}],
    }
    if system_prompt:
        body["system"] = system_prompt
    return body


def build_nova_body(prompt: str, max_tokens: int, system_prompt: str = "") -> dict:
    """
    Amazon Nova — invoke_model schema.
    Nova uses messages array with inferenceConfig (NOT max_tokens at top level).
    Ref: https://docs.aws.amazon.com/nova/latest/userguide/complete-request-schema.html
    """
    body: dict = {
        "messages": [
            {
                "role":    "user",
                "content": [{"text": prompt}],
            }
        ],
        "inferenceConfig": {
            "maxTokens":   max_tokens,
            "temperature": 0.1,
            "topP":        0.9,
        },
    }
    if system_prompt:
        body["system"] = [{"text": system_prompt}]
    return body


def build_titan_body(prompt: str, max_tokens: int, system_prompt: str = "") -> dict:
    """Amazon Titan Text models."""
    full_prompt = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
    return {
        "inputText": full_prompt,
        "textGenerationConfig": {
            "maxTokenCount": max_tokens,
            "temperature":   0.0,
            "topP":          0.9,
        },
    }


def build_llama_body(prompt: str, max_tokens: int, system_prompt: str = "") -> dict:
    """Meta Llama models via Bedrock."""
    full_prompt = (
        f"<s>[INST] <<SYS>>{system_prompt}<</SYS>>\n\n{prompt} [/INST]"
        if system_prompt
        else f"<s>[INST] {prompt} [/INST]"
    )
    return {
        "prompt":      full_prompt,
        "max_gen_len": max_tokens,
        "temperature": 0.0,
    }


def build_mistral_body(prompt: str, max_tokens: int, system_prompt: str = "") -> dict:
    """Mistral models via Bedrock."""
    full_prompt = (
        f"<s>[INST] {system_prompt}\n\n{prompt} [/INST]"
        if system_prompt
        else f"<s>[INST] {prompt} [/INST]"
    )
    return {
        "prompt":      full_prompt,
        "max_tokens":  max_tokens,
        "temperature": 0.0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Response parsers (one per provider family)
# ─────────────────────────────────────────────────────────────────────────────

def parse_claude_response(response_body: dict) -> str:
    """Extract text from Claude Messages API response."""
    content = response_body.get("content", [])
    if content and isinstance(content, list):
        return content[0].get("text", "")
    return response_body.get("completion", "")


def parse_nova_response(response_body: dict) -> str:
    """
    Extract text from Amazon Nova invoke_model response.
    Nova returns: {"output": {"message": {"role": "assistant", "content": [{"text": "..."}]}}}
    """
    # Primary path: output.message.content[0].text
    try:
        output = response_body.get("output", {})
        message = output.get("message", {})
        content = message.get("content", [])
        if content and isinstance(content, list):
            text = content[0].get("text", "")
            if text:
                return text
    except (KeyError, IndexError, TypeError):
        pass
    # Fallback paths for different Nova versions
    if "outputText" in response_body:
        return response_body["outputText"]
    # Last resort: stringify the whole body
    logger.warning("Could not parse Nova response — dumping body: %s", str(response_body)[:200])
    return str(response_body)


def parse_titan_response(response_body: dict) -> str:
    """Extract text from Amazon Titan response."""
    results = response_body.get("results", [{}])
    if results:
        return results[0].get("outputText", "")
    return response_body.get("outputText", "")


def parse_llama_response(response_body: dict) -> str:
    """Extract text from Meta Llama response."""
    return response_body.get("generation", "")


def parse_mistral_response(response_body: dict) -> str:
    """Extract text from Mistral response."""
    outputs = response_body.get("outputs", [{}])
    if outputs:
        return outputs[0].get("text", "")
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Unified adapter
# ─────────────────────────────────────────────────────────────────────────────

# Dispatch tables
BODY_BUILDERS = {
    "claude":  build_claude_body,
    "nova":    build_nova_body,
    "titan":   build_titan_body,
    "llama":   build_llama_body,
    "mistral": build_mistral_body,
    "unknown": build_claude_body,   # safe default
}

RESPONSE_PARSERS = {
    "claude":  parse_claude_response,
    "nova":    parse_nova_response,
    "titan":   parse_titan_response,
    "llama":   parse_llama_response,
    "mistral": parse_mistral_response,
    "unknown": parse_claude_response,   # safe default
}


class BedrockModelAdapter:
    """
    Unified interface for calling any supported Bedrock model.

    Supports:
      - anthropic.claude-3-sonnet-20240229-v1:0
      - anthropic.claude-3-haiku-20240307-v1:0
      - anthropic.claude-3-5-sonnet-20241022-v2:0
      - anthropic.claude-opus-4-1-20250805-v1:0  (auto-resolves profile)
      - amazon.nova-pro-v1:0
      - amazon.nova-lite-v1:0
      - amazon.nova-micro-v1:0
      - amazon.titan-text-express-v1
      - meta.llama3-70b-instruct-v1:0
      - mistral.mistral-large-2402-v1:0

    Parameters
    ----------
    region_name  : AWS region
    max_retries  : Number of retries on throttling (exponential backoff)
    """

    def __init__(
        self,
        region_name: str = "us-east-1",
        max_retries: int = 3,
    ):
        self.region      = region_name
        self.max_retries = max_retries
        self._client     = boto3.client("bedrock-runtime", region_name=region_name)
        logger.info("BedrockModelAdapter initialised (region=%s)", region_name)

    def invoke(
        self,
        model_id:      str,
        prompt:        str,
        max_tokens:    int  = 1024,
        system_prompt: str  = "",
    ) -> str:
        """
        Invoke any supported Bedrock model and return the text response.

        Parameters
        ----------
        model_id      : Bedrock model ID or inference profile ARN
        prompt        : User prompt text
        max_tokens    : Maximum tokens in response
        system_prompt : Optional system/instruction prompt

        Returns
        -------
        str — the model's text response

        Raises
        ------
        ClientError — if the call fails after all retries
        """
        family          = detect_model_family(model_id)
        resolved_id     = resolve_model_id(model_id, self.region)
        build_body      = BODY_BUILDERS.get(family, build_claude_body)
        parse_response  = RESPONSE_PARSERS.get(family, parse_claude_response)

        request_body = build_body(prompt, max_tokens, system_prompt)
        logger.debug("Invoking model=%s (family=%s)", resolved_id, family)

        last_error = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self._client.invoke_model(
                    modelId     = resolved_id,
                    contentType = "application/json",
                    accept      = "application/json",
                    body        = json.dumps(request_body),
                )
                response_body = json.loads(response["body"].read())
                text = parse_response(response_body)
                logger.debug("Model response received (%d chars)", len(text))
                return text

            except ClientError as exc:
                error_code = exc.response["Error"]["Code"]
                # Retry on throttling
                if error_code == "ThrottlingException" and attempt < self.max_retries:
                    wait = 2 ** attempt
                    logger.warning("Throttled on attempt %d — retrying in %ds", attempt, wait)
                    time.sleep(wait)
                    last_error = exc
                    continue
                # Re-raise all other errors
                raise

        raise last_error  # type: ignore

    def invoke_json(
        self,
        model_id:      str,
        prompt:        str,
        max_tokens:    int  = 1024,
        system_prompt: str  = "",
    ) -> dict:
        """
        Same as invoke() but parses the response as JSON.
        Strips markdown code fences if present.
        Falls back to {"raw_response": text} if JSON parsing fails.
        """
        text = self.invoke(model_id, prompt, max_tokens, system_prompt)

        # Strip markdown fences
        clean = text.strip()
        if clean.startswith("```"):
            lines = clean.split("\n")
            clean = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

        try:
            return json.loads(clean)
        except json.JSONDecodeError:
            logger.warning("Model response was not valid JSON — returning raw")
            return {"raw_response": text}


# ─────────────────────────────────────────────────────────────────────────────
# Convenience function (drop-in for agents)
# ─────────────────────────────────────────────────────────────────────────────

_adapter_cache: dict[str, BedrockModelAdapter] = {}


def get_adapter(region_name: str = "us-east-1") -> BedrockModelAdapter:
    """Return a cached BedrockModelAdapter for the given region."""
    if region_name not in _adapter_cache:
        _adapter_cache[region_name] = BedrockModelAdapter(region_name=region_name)
    return _adapter_cache[region_name]
