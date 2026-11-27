# Test fixtures

`sample_repo/` is a tiny fake repository used by the tests. It is read as text
only and never executed. `app/utils.py` contains an intentional command
injection (`shell=True` with user input) so tests have a real issue to find.