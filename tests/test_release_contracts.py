import os
import re
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts: str) -> str:
    path = os.path.join(REPO_ROOT, *parts)
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


class TestCIReleaseWorkflowContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ci_workflow = _read(".github", "workflows", "ci.yml")
        cls.release_workflow = _read(".github", "workflows", "release.yml")
        cls.release_doc = _read("docs", "release.md")
        cls.configuration_doc = _read("docs", "configuration.md")
        cls.lambda_example = _read("deploy", "examples", "lambda-sqs-consumer.yaml")

    def test_ci_builds_lambda_image(self):
        self.assertIn("- image: cleanarr-lambda", self.ci_workflow)
        self.assertIn("file: apps/lambda/Dockerfile", self.ci_workflow)

    def test_release_gates_are_enforced(self):
        self.assertRegex(self.release_workflow, r'test "\$\{ARCH_REVIEW_TAG\}" = "\$\{RELEASE_TAG\}"')
        self.assertRegex(self.release_workflow, r'test "\$\{SEC_REVIEW_TAG\}" = "\$\{RELEASE_TAG\}"')
        self.assertIn("Missing review gate tag variables.", self.release_workflow)

    def test_release_matrix_covers_lambda_sqs_promotion_contract(self):
        lambda_block = re.search(
            r"image_name:\s*cleanarr-lambda\s*\n\s*dockerfile:\s*apps/lambda/Dockerfile\s*\n\s*ecr_repository:\s*cleanarr-job",
            self.release_workflow,
            re.MULTILINE,
        )
        self.assertIsNotNone(lambda_block)

        self.assertIn('"ecr_release_tag_ref"', self.release_workflow)
        self.assertIn('"ecr_repository":', self.release_workflow)

    def test_release_documentation_mentions_lambda_sqs_contract(self):
        self.assertIn("Downstream Lambda + SQS deployment contract", self.release_doc)
        self.assertIn("FunctionResponseTypes: [ReportBatchItemFailures]", self.release_doc)

    def test_configuration_documents_lambda_queue_consumer_contract(self):
        self.assertIn("AWS Lambda SQS consumer contract", self.configuration_doc)
        self.assertIn("CLEANARR_WEBHOOK_QUEUE_POLLING=false", self.configuration_doc)

    def test_lambda_sqs_example_exists_with_expected_contract_fields(self):
        self.assertIn("AWS::Lambda::EventSourceMapping", self.lambda_example)
        self.assertIn("FunctionResponseTypes:", self.lambda_example)
        self.assertIn("ReportBatchItemFailures", self.lambda_example)
        self.assertIn("cleanarr-webhook-consumer", self.lambda_example)
        self.assertIn("CLEANARR_WEBHOOK_QUEUE_MODE: sqs", self.lambda_example)
        self.assertIn("ecr_release_tag_ref", self.lambda_example)


if __name__ == "__main__":
    unittest.main()
