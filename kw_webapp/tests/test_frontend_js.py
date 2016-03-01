import os
from casper.tests import CasperTestCase

from kw_webapp.tests.utils import create_user

test_file_path = os.path.join(os.path.dirname(__file__), "..", "..", "_front-end/test/sample-test.js")

class AllFrontEndTests(CasperTestCase):

    def setUp(self):
        #TODO @Subversity This is the rough outline of how to create a user. This "setUp" function is called before every
        # Single test is run. Any data you add to database in this method will be emptied out at the end of EVERY test.
        # The overall way a test runs is. setUp() -> test() -> tearDown()
        self.user = create_user("duncantest1")
        self.user.set_password("dadedade")
        self.user.save()

    def tearDown(self):
        pass

    def test_index(self):
        print("TESTING JS TESTS")
        self.assertTrue(self.casper(test_file_path))


