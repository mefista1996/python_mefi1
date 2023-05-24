user_input = input("Please input all the necessary numbers to add").split()
for n in range(0, len(user_input)):
    user_input[n] = int(user_input[n])

total = 0
for number in range(0, len(user_input), 2):
    total += number

print(total)