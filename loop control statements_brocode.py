 #loop control - change a loop from it's normal sequence

 #break - stops the loop
 #continue - skips the next iteration of the loop
 #pass - does nothing, acts like a placeholder

while True:
    name = input("Name?: ")
    if name != "":
        break
while True:
    lst_name = input("LS:?")
    if lst_name != "":
        break
print(name + lst_name)

phone_number = "123-456-7890"
for i in phone_number:
    if i == "-":
        continue
    print(i, end = "")

for i in range(1,21):
    if i == 13:
        pass
    else:
        print(i)