#!/usr/bin/env python3

import argparse
import base64
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from openai import OpenAI


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "post"


def strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()


def extract_json_object(text: str) -> dict:
    text = strip_code_fences(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def ensure_dirs(base_dir: Path):
    (base_dir / "drafts").mkdir(parents=True, exist_ok=True)
    (base_dir / "images").mkdir(parents=True, exist_ok=True)
    (base_dir / "logs").mkdir(parents=True, exist_ok=True)


def save_text(path: Path, content: str):
    path.write_text(content, encoding="utf-8")


def save_json(path: Path, data: dict):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def save_bytes(path: Path, content: bytes):
    path.write_bytes(content)


def generate_content_plan(
    client,
    model,
    topic,
    research=None
):
    if research:
        research_context = f"""
RESEARCH CONTEXT:

{research.get("research_summary", "")}

Sources:
{research.get("sources", [])}

Use this research as the factual basis for any current or
time-sensitive claims.

Do not invent facts that are not supported by the research.
"""
    else:
        research_context = """
No external research was supplied.

Write a practical post based on the supplied topic and general
engineering principles.

Do not invent specific version numbers, announcements, dates,
benchmarks or current product changes.
"""

    prompt = f"""
Create a professional LinkedIn post about:

{topic}

{research_context}

AUTHOR POSITIONING:

The author is an experienced infrastructure / IT engineer developing
deeper hands-on DevOps, cloud and automation skills.

The author has practical lab and personal-project experience with
Jenkins, Docker, Kubernetes, Terraform, Ansible, AWS, Prometheus,
Grafana, Python and GitHub.

IMPORTANT:
Never imply that the author operated something in enterprise
production unless that experience is explicitly supplied.

Correctly distinguish between:
- professional experience
- hands-on lab/project experience
- researched industry developments

WRITING STYLE:

Choose ONE of these post formats naturally:

A. Practical lesson from a project
B. Architecture explanation
C. Troubleshooting story:
   problem -> investigation -> cause -> fix -> lesson
D. Engineering observation or opinion
E. Recent technology/change analysis
F. "How I built this" project update
G. Technology comparison and appropriate use cases

Do NOT use the same format repeatedly.

Avoid repetitive phrases such as:
- "My practical checklist"
- "For me..."
- "I would..." in every paragraph
- "Here are 5 things..."
- generic motivational conclusions

Write naturally, as an engineer sharing something useful with other
technical professionals.

Prefer:
- concrete engineering examples
- lessons learned
- trade-offs
- failure scenarios
- architecture decisions
- troubleshooting
- operational considerations

POST REQUIREMENTS:

- approximately 180-350 words
- suitable for LinkedIn
- readable short paragraphs
- no markdown table
- do not overuse emojis
- 3-6 relevant hashtags at the end
- do not mention being an AI
- do not ask the reader to provide missing information
- never output a refusal or research warning as the post

IMAGE:

Create an image concept directly related to the post.

For DevOps/infrastructure topics:
- professional enterprise technology visual
- architecture, pipeline or infrastructure concepts where relevant
- navy / blue / teal aesthetic
- minimal readable text
- 16:9 LinkedIn-friendly composition
- no official vendor logos

Return ONLY valid JSON:

{{
  "post_text": "...",
  "image_prompt": "...",
  "alt_text": "...",
  "image_title": "..."
}}
"""

    response = client.responses.create(
        model=model,
        input=prompt,
    )

    return extract_json_object(response.output_text)

def generate_image_bytes(client: OpenAI, image_model: str, prompt: str) -> bytes:
    result = client.images.generate(
        model=image_model,
        prompt=prompt,
        size="1536x1024",
        quality="high",
    )

    item = result.data[0]
    b64_data = getattr(item, "b64_json", None)
    if b64_data is None and isinstance(item, dict):
        b64_data = item.get("b64_json")

    if not b64_data:
        raise RuntimeError("OpenAI image generation did not return b64_json")

    return base64.b64decode(b64_data)


def linkedin_headers(token: str, version: str, json_content: bool = True) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Linkedin-Version": version,
        "X-Restli-Protocol-Version": "2.0.0",
    }
    if json_content:
        headers["Content-Type"] = "application/json"
    return headers


def initialize_linkedin_image_upload(token: str, version: str, owner_urn: str) -> tuple[str, str]:
    url = "https://api.linkedin.com/rest/images?action=initializeUpload"
    payload = {
        "initializeUploadRequest": {
            "owner": owner_urn
        }
    }

    r = requests.post(
        url,
        headers=linkedin_headers(token, version, json_content=True),
        json=payload,
        timeout=60,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"LinkedIn initializeUpload failed: {r.status_code} {r.text}")

    data = r.json()
    upload_url = data["value"]["uploadUrl"]
    image_urn = data["value"]["image"]
    return upload_url, image_urn


def upload_image_to_linkedin(upload_url: str, image_bytes: bytes):
    # LinkedIn DMS uploads commonly work with raw PUT.
    put_resp = requests.put(
        upload_url,
        headers={"Content-Type": "image/png"},
        data=image_bytes,
        timeout=180,
    )

    if put_resp.status_code < 300:
        return

    # fallback in case endpoint expects POST instead
    post_resp = requests.post(
        upload_url,
        headers={"Content-Type": "image/png"},
        data=image_bytes,
        timeout=180,
    )
    if post_resp.status_code >= 300:
        raise RuntimeError(
            f"LinkedIn image upload failed. PUT={put_resp.status_code} {put_resp.text} "
            f"POST={post_resp.status_code} {post_resp.text}"
        )


def create_linkedin_post(
    token: str,
    version: str,
    author_urn: str,
    commentary: str,
    image_urn: str | None = None,
    alt_text: str | None = None,
    image_title: str | None = None,
) -> tuple[str | None, dict | None]:
    url = "https://api.linkedin.com/rest/posts"

    payload = {
        "author": author_urn,
        "commentary": commentary,
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": []
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False
    }

    if image_urn:
        payload["content"] = {
            "media": {
                "id": image_urn
            }
        }
        if alt_text:
            payload["content"]["media"]["altText"] = alt_text
        if image_title:
            payload["content"]["media"]["title"] = image_title

    r = requests.post(
        url,
        headers=linkedin_headers(token, version, json_content=True),
        json=payload,
        timeout=60,
    )

    if r.status_code != 201:
        raise RuntimeError(f"LinkedIn post creation failed: {r.status_code} {r.text}")

    post_id = r.headers.get("x-restli-id")
    body = None
    if r.text.strip():
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text}

    return post_id, body

def generate_fresh_topic(client, model, recent_topics):
    recent_topics_text = "\n".join(
        f"- {topic}" for topic in recent_topics
    ) or "None"

    prompt = f"""
You are selecting ONE LinkedIn topic for a professional focused on
DevOps, Cloud, Infrastructure and Automation.

The author's background includes hands-on work with:
- Jenkins and CI/CD
- Docker
- Kubernetes
- Terraform
- Ansible
- AWS and Azure
- Prometheus and Grafana
- Linux
- Python automation
- GitHub
- self-hosting and infrastructure troubleshooting

Choose ONE topic from these categories:

1. Jenkins / CI/CD
2. Docker / containers
3. Kubernetes / cloud native
4. Terraform / Infrastructure as Code
5. Ansible / configuration management
6. AWS / Azure cloud engineering
7. Prometheus / Grafana / observability
8. Linux / infrastructure operations
9. DevSecOps / software supply-chain security
10. Python / automation
11. Git / GitHub / software delivery
12. Real-world troubleshooting lessons
13. Self-hosting / homelab engineering
14. Platform engineering / SRE

CONTENT DIVERSITY RULES:

- Do NOT select the same primary technology as any of the last 3 posts.
- Do not allow Kubernetes to dominate the content.
- Kubernetes should represent roughly no more than 20% of posts over time.
- Avoid several consecutive release-note posts from the same product.
- Prefer topics with practical engineering value.
- Prefer architecture, troubleshooting, automation, CI/CD,
  observability and lessons learned.
- A topic does NOT need to be breaking news.
- If there is no worthwhile recent development, choose a useful
  evergreen engineering topic instead.
- Do not select a topic that is substantially similar to recent topics.

Recent topics:

{recent_topics_text}

Research the selected topic using web search when it concerns a recent
release, product change, security issue or other time-sensitive fact.

If the topic is evergreen and does not depend on current information,
sources may be empty.

If the topic is time-sensitive, sources MUST contain at least one
credible source.

Return ONLY valid JSON in this structure:

{{
  "topic": "short topic title",
  "category": "one category from the list above",
  "research_summary": "concise factual research summary",
  "sources": [
    {{
      "title": "source title",
      "url": "https://..."
    }}
  ]
}}
"""

    response = client.responses.create(
        model=model,
        tools=[{"type": "web_search"}],
        input=prompt,
    )

    return extract_json_object(response.output_text)

def load_recent_topics(base_dir, limit=20):
    history_file = base_dir / "topic-history.txt"

    if not history_file.exists():
        return []

    topics = history_file.read_text(
        encoding="utf-8"
    ).splitlines()

    topics = [
        topic.strip()
        for topic in topics
        if topic.strip()
    ]

    return topics[-limit:]


def append_topic_history(base_dir, topic):
    history_file = base_dir / "topic-history.txt"

    with open(history_file, "a", encoding="utf-8") as f:
        f.write(topic.strip() + "\n")

def validate_generated_post(content_plan, research=None, auto_topic=False):
    post_text = content_plan.get("post_text", "").strip()

    if not post_text:
        raise RuntimeError("Generated post text is empty")

    bad_phrases = [
        "can't responsibly",
        "cannot responsibly",
        "research section is empty",
        "please provide",
        "i need more information",
        "i don't have enough information",
        "unable to create",
        "cannot create this post",
    ]

    lower_post = post_text.lower()

    for phrase in bad_phrases:
        if phrase in lower_post:
            raise RuntimeError(
                f"Generated post failed validation: '{phrase}'"
            )

    if len(post_text) < 300:
        raise RuntimeError(
            "Generated post is unusually short and requires review"
        )

    if auto_topic:
        if not research:
            raise RuntimeError(
                "Auto-topic mode returned no research object"
            )

        if not research.get("research_summary"):
            raise RuntimeError(
                "Auto-topic research summary is empty"
            )

    return True

def main():
    parser = argparse.ArgumentParser(description="Generate and optionally publish LinkedIn posts with OpenAI + LinkedIn.")
    parser.add_argument(
    "--auto-topic",
    action="store_true",
    help="Automatically find a recent DevOps topic"
    )
    parser.add_argument("--topic", help="Topic for the LinkedIn post")
    parser.add_argument("--draft-json", help="Existing approved draft JSON to publish")
    parser.add_argument("--image-file", help="Existing approved image to publish")
    parser.add_argument("--publish", action="store_true", help="Actually publish to LinkedIn")
    parser.add_argument("--text-only", action="store_true", help="Skip image generation and publish text only")
    parser.add_argument("--base-dir", default=str(Path.home() / "linkedin-automation"), help="Working directory")
    args = parser.parse_args()
    #if not args.topic and not args.draft_json:
    #    parser.error("Provide either --topic or --draft-json")
    if not args.topic and not args.draft_json and not args.auto_topic:
        parser.error(
            "Provide --topic, --auto-topic, or --draft-json"
        )

    openai_api_key = require_env("OPENAI_API_KEY")
    linkedin_token = require_env("LINKEDIN_TOKEN")
    author_urn = os.getenv("LINKEDIN_URN")
    if not author_urn:
        member_id = require_env("LINKEDIN_MEMBER_ID")
        author_urn = f"urn:li:person:{member_id}"

    linkedin_version = os.getenv("LINKEDIN_VERSION", "202608")
    text_model = os.getenv("OPENAI_TEXT_MODEL", "gpt-5.6")
    image_model = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-2")

    base_dir = Path(args.base_dir).expanduser()
    ensure_dirs(base_dir)

    client = OpenAI(api_key=openai_api_key)

    image_bytes = None
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    if args.draft_json:
        print("[1/5] Loading approved draft...")

        draft_json_path = Path(args.draft_json)

        with open(draft_json_path, "r", encoding="utf-8") as f:
            plan = json.load(f)

        draft_text_path = draft_json_path.with_suffix(".txt")

        if args.image_file:
            image_path = Path(args.image_file)
            image_bytes = image_path.read_bytes()
        else:
            image_path = None
            image_bytes = None

        publish_log_path = (
            base_dir / "logs" /
            f"{timestamp}-approved-draft-publish.json"
        )

    else:
        research = None

        if args.auto_topic:
            print("[0/5] Researching recent DevOps topic...")

            recent_topics = load_recent_topics(
                base_dir,
                limit=20
            )

            research = generate_fresh_topic(
                client,
                text_model,
                recent_topics
            )

            args.topic = research["topic"]

            print(f"Selected topic: {args.topic}")
            print(f"Sources found: {len(research.get('sources', []))}")
        topic_slug = slugify(args.topic)[:50]
        prefix = f"{timestamp}-{topic_slug}"

        draft_json_path = base_dir / "drafts" / f"{prefix}.json"
        draft_text_path = base_dir / "drafts" / f"{prefix}.txt"
        image_path = base_dir / "images" / f"{prefix}.png"
        publish_log_path = base_dir / "logs" / f"{prefix}-publish.json"

        print("[1/5] Generating post plan with OpenAI...")

        plan = generate_content_plan(
            client=client,
            model=text_model,
            topic=args.topic,
            research=research,
        )
        
        validate_generated_post(
            plan,
            research=research,
            auto_topic=args.auto_topic
        )
        if research:
            plan["research"] = research

        save_json(draft_json_path, plan)
        save_text(draft_text_path, plan["post_text"])

        image_bytes = None

        if not args.text_only:
            print("[2/5] Generating image with OpenAI...")

            image_bytes = generate_image_bytes(
                client,
                image_model=image_model,
                prompt=plan["image_prompt"]
            )

            save_bytes(image_path, image_bytes)

        append_topic_history(
            base_dir,
            args.topic
        )

    print("\n=== GENERATED POST ===\n")
    print(plan["post_text"])
    print("\n======================\n")
    print(f"Draft JSON saved to: {draft_json_path}")
    print(f"Draft text saved to: {draft_text_path}")
    if image_bytes:
        print(f"Image saved to:      {image_path}")

    if not args.publish:
        print("\nDry run only. Nothing was published.")
        print("To publish, rerun with --publish")
        return

    print(f"[3/5] Publishing to LinkedIn...")
    image_urn = None

    if image_bytes:
        print(" - Initializing LinkedIn image upload...")
        upload_url, image_urn = initialize_linkedin_image_upload(
            token=linkedin_token,
            version=linkedin_version,
            owner_urn=author_urn,
        )
        print(f" - Uploading image to LinkedIn DMS...")
        upload_image_to_linkedin(upload_url, image_bytes)
        print(f" - LinkedIn image URN: {image_urn}")
        # brief pause to give LinkedIn processing time
        time.sleep(3)

    post_id, post_body = create_linkedin_post(
        token=linkedin_token,
        version=linkedin_version,
        author_urn=author_urn,
        commentary=plan["post_text"],
        image_urn=image_urn,
        alt_text=plan.get("alt_text"),
        image_title=plan.get("image_title"),
    )

    publish_result = {
        "topic": args.topic,
        "author_urn": author_urn,
        "linkedin_version": linkedin_version,
        "post_id": post_id,
        "image_urn": image_urn,
        "draft_json_path": str(draft_json_path),
        "draft_text_path": str(draft_text_path),
        "image_path": str(image_path) if image_bytes else None,
        "response_body": post_body,
        "published_at": datetime.now().isoformat(),
    }
    save_json(publish_log_path, publish_result)

    print(f"[4/5] Published successfully")
    print(f"Post ID: {post_id}")
    print(f"Publish log saved to: {publish_log_path}")
    print(f"[5/5] Done")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)
