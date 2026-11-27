"""Calculate overview fields for the public repositories in the Metrics inventory."""

import json
import os
import subprocess
from collections import Counter
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def container_count(owner, repositories, namespaces):
    count = 0
    for login, kind in namespaces.items():
        prefix = "orgs" if kind == "Organization" else "users"
        url = (f"https://api.github.com/{prefix}/{login}/packages?package_type=container"
               "&visibility=public&per_page=100")
        while url:
            request = Request(url, headers={"Authorization": "Bearer " + os.environ["PACKAGES_TOKEN"]})
            try:
                response = urlopen(request, timeout=30)
            except HTTPError as error:
                if error.code not in (400, 401, 403, 404):
                    raise
                print(f"::warning::Package inventory unavailable for {login} (HTTP {error.code}).")
                return None
            with response:
                for package in json.load(response):
                    repo = package.get("repository") or {}
                    if login == owner or repo.get("full_name") in repositories:
                        count += 1
                links = response.headers.get("Link", "").split(",")
                url = next((link.split(">")[0].strip(" <") for link in links if 'rel="next"' in link), None)
    return count


owner = os.environ["GITHUB_REPOSITORY_OWNER"]
query = """query($login:String!,$endCursor:String) { user(login:$login) {
  repositories(first:100,after:$endCursor,privacy:PUBLIC,ownerAffiliations:[OWNER,COLLABORATOR,ORGANIZATION_MEMBER]){
    pageInfo{hasNextPage endCursor}
    nodes{nameWithOwner owner{login __typename} releases{totalCount} packages{totalCount} licenseInfo{spdxId}}
  }
} }"""
pages = json.loads(subprocess.check_output(["gh", "api", "graphql", "--paginate", "--slurp",
                                            "-f", f"query={query}", "-f", f"login={owner}"]))
repos = [repo for page in pages for repo in page["data"]["user"]["repositories"]["nodes"]]
licenses = Counter((repo["licenseInfo"] or {}).get("spdxId") for repo in repos)
for unknown in (None, "", "Other", "NOASSERTION", "NONE"):
    licenses.pop(unknown, None)
favorite = min(licenses, key=lambda name: (-licenses[name], name)) if licenses else None
namespaces = {owner: "User"}
namespaces.update({repo["owner"]["login"]: repo["owner"]["__typename"] for repo in repos})
containers = container_count(owner, {repo["nameWithOwner"] for repo in repos}, namespaces)
linked_packages = sum(repo["packages"]["totalCount"] for repo in repos)
stats = {"releases": sum(repo["releases"]["totalCount"] for repo in repos),
         "packages": linked_packages + (containers or 0), "containers_checked": containers is not None,
         "license": favorite}
with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf8") as output:
    print("stats=" + json.dumps(stats), file=output)
