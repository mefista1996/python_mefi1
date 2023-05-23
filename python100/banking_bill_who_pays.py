# Import the random module here
import random
# Split string method
names_string = input("Give me everybody's names, separated by a comma. ")
names = names_string.split(", ")
# 🚨 Don't change the code above 👆

# #Write your code below this line 👇

x = len(names)
randomize = random.randint(0, x - 1)
payer = names[randomize]
#
print(f"{payer} is going to buy the meal today!")
