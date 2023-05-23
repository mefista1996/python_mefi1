#Write a program to keep asking for
#a number until you enter a negative number. At the end,
#print the sum of all entered numbers.

sumaa = []
num = int(input("Please enter your number: "))
sumaa.append(num)
while num > -1:
    print("Nope, that's not it")
    num = int(input("Please enter your number: "))
    sumaa.append(num)
else:
    print("You got it")
    print("Total sum of your responses", "=", sum(sumaa))





