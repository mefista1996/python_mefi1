import sys

inputs = sys.argv
inputs.pop(0)

for item in inputs:
    item = int(item)

    if item % 3 == 0 and item % 5 == 0:
        print("fizzbuzz")
    elif item % 3 == 0:
        print('fizz')
    elif item % 5 == 0:
        print('buzz')
    elif item % 3 != 0:
        print (item)
    elif item % 5 != 0:
        print(item)



