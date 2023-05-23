import sys

first_argument = sys.argv[1]
python_is_glorious = True
failure_is_option = False
proper_greeting = False
if first_argument == "For the glory of Python!":
    proper_greeting = True
    print(proper_greeting)
else:
    print('Nope')