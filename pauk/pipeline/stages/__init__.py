from .author_names import AuthorNamesStage
from .code_links import CodeLinksStage
from .dedup import DedupStage
from .departments import DepartmentsStage
from .emails import EmailsStage
from .github_match import GitHubMatchStage
from .link_relevance import LinkRelevanceStage
from .persons import PersonsStage
from .repo_people import RepoPeopleStage
from .repositories import RepositoriesStage
from .social_graph import SocialGraphStage

# A chain of dependencies: dedup folds on what the fetching stages brought,
# names follow it, link_relevance follows code_links, emails precedes
# github_match, and github_match follows the repositories harvest.
ALL_STAGES = (
    PersonsStage, DepartmentsStage, CodeLinksStage, LinkRelevanceStage,
    EmailsStage, RepositoriesStage, RepoPeopleStage, DedupStage, GitHubMatchStage,
    AuthorNamesStage,
)
# Out of the default run: hundreds of API calls that pay off only once
# github_match has confirmed some accounts to walk outward from.
OPTIONAL_STAGES = (SocialGraphStage,)
